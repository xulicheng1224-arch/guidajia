"""Stagger browser starts within an account without serializing whole crawls."""

import asyncio
import random
from datetime import timedelta

from . import store as s, domain as d

_ACCOUNTS = {}


def _mark(job, until=None):
    with s.connection() as db:
        if until is False:
            db.execute("UPDATE jobs SET context=json_remove(context,'$.pacing') WHERE id=?", (job["id"],))
        else:
            value = {"message": "等待当前账号的商品启动间隔", "resume_at": until}
            db.execute("UPDATE jobs SET context=json_set(context,'$.pacing',json(?)) WHERE id=? AND status='running'", (s.dumps(value), job["id"]))


async def wait_turn(job, gap, ready):
    """Waiting is outside the browser's 120s budget. Cancellation reserves no turn."""
    if gap <= 0:
        return 0 if ready() else None
    loop = asyncio.get_running_loop()
    key = (str(s.data_dir()), loop)
    entry = _ACCOUNTS.setdefault(key, {"lock": asyncio.Lock(), "next": 0})
    started = loop.time()
    _mark(job)
    try:
        async with entry["lock"]:
            if not ready():
                return None
            delay = max(0, entry["next"] - loop.time())
            if delay:
                _mark(job, (d.utcnow() + timedelta(seconds=delay)).isoformat())
                await asyncio.sleep(delay)
            # Another task may have encountered a challenge, the user may have
            # opened login, paused timers, or changed sources while we waited.
            if not ready():
                return None
            entry["next"] = loop.time() + gap + random.uniform(0, min(2, gap * .25))
            return int((loop.time() - started) * 1000)
    finally:
        _mark(job, False)
