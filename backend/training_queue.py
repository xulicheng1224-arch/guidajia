"""Durable FIFO training requests; one actual training thread server-wide."""

import asyncio
import fcntl
import json
import threading
import weakref
from dataclasses import dataclass, field

from . import accounts, store as s

EXECUTION_LOCK = threading.RLock()
_LOOPS = weakref.WeakKeyDictionary()


@dataclass
class Coordinator:
    runner: object = None
    futures: dict = field(default_factory=dict)


def coordinator():
    loop = asyncio.get_running_loop()
    return _LOOPS.setdefault(loop, {}).setdefault(str(s.DATA), Coordinator())


def recover():
    with accounts.connection() as db:
        db.execute("UPDATE training_queue SET state='queued',started_at=NULL WHERE state='running'")


def status(user_id=None):
    user_id = user_id or s.TENANT.get()
    with accounts.connection() as db:
        rows = db.execute("SELECT id,user_id,state,requested_at,started_at FROM training_queue WHERE state IN ('queued','running') ORDER BY requested_at,id").fetchall()
    queued = [row for row in rows if row["state"] == "queued"]
    own = next((row for row in rows if row["user_id"] == user_id), None)
    return {
        "state": own["state"] if own else "idle", "position": next((i + 1 for i, row in enumerate(queued) if row["user_id"] == user_id), None),
        "requested_at": own["requested_at"] if own else None,
        "started_at": own["started_at"] if own else None,
        "global_limit": 1, "global_running": any(row["state"] == "running" for row in rows),
    }


def enqueue(configuration):
    owner = s.TENANT.get()
    with accounts.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT id FROM training_queue WHERE user_id=? AND state IN ('queued','running')", (owner,)).fetchone()
        if row:
            return row[0], False
        job_id = s.uid()
        db.execute("INSERT INTO training_queue VALUES(?,?,'queued',?,NULL,NULL,?,NULL)", (job_id, owner, s.now(), configuration))
    return job_id, True


def execute(owner):
    from . import prediction
    # The file lock also protects against overlapping CLI/server processes.
    with EXECUTION_LOCK, (s.DATA / "training-global.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with s.tenant_scope(owner):
            return prediction.train()


async def process(state):
    while True:
        with accounts.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # Disabled users retain their data but do not consume a training slot.
            db.execute("UPDATE training_queue SET state='cancelled',finished_at=? WHERE state='queued' AND user_id IN (SELECT id FROM users WHERE disabled=1)", (s.now(),))
            if db.execute("SELECT 1 FROM training_queue WHERE state='running'").fetchone():
                return
            row = db.execute("SELECT * FROM training_queue WHERE state='queued' ORDER BY requested_at,id LIMIT 1").fetchone()
            if not row:
                return
            db.execute("UPDATE training_queue SET state='running',started_at=? WHERE id=?", (s.now(), row["id"]))
        work = asyncio.create_task(asyncio.to_thread(execute, row["user_id"]))
        cancelled = False
        try:
            try:
                result = await asyncio.shield(work)
            except asyncio.CancelledError:
                # Cancelling to_thread does not stop its thread. Keep the slot
                # until the real work finishes, before allowing another run.
                cancelled = True
                result = await work
        except Exception as error:
            result = {"state": "error", "error_type": type(error).__name__}
        with accounts.connection() as db:
            db.execute("UPDATE training_queue SET state='finished',finished_at=?,result=? WHERE id=?", (s.now(), json.dumps(result), row["id"]))
        future = state.futures.pop(row["id"], None)
        if future and not future.done():
            future.set_result(result)
        if cancelled:
            raise asyncio.CancelledError


def kick():
    state = coordinator()
    if not state.runner or state.runner.done():
        state.runner = asyncio.create_task(process(state))


async def wait(job_id):
    state = coordinator()
    future = state.futures.setdefault(job_id, asyncio.get_running_loop().create_future())
    try:
        while True:
            with accounts.connection() as db:
                row = db.execute("SELECT state,result FROM training_queue WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] in ("finished", "cancelled"):
                return json.loads(row["result"]) if row and row["result"] else {"state": "cancelled"}
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=0.25)
            except asyncio.TimeoutError:
                pass
    finally:
        state.futures.pop(job_id, None)


async def shutdown():
    state = coordinator()
    if state.runner and not state.runner.done():
        state.runner.cancel()
        await asyncio.gather(state.runner, return_exceptions=True)
    for future in state.futures.values():
        future.cancel()
    state.futures.clear()
