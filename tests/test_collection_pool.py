"""Concurrency, timeout, process cleanup and shared Cookie regression checks."""

import asyncio
import json
import os
import sys
from pathlib import Path

import psutil
import pytest

from backend import collector, domain as d, session_state, store as s, worker
from tests.test_system import client, product


def queued_products(client, count):
    # Concurrency/process tests exclude the wall-clock spacing tested separately.
    s.set_settings({"crawler_start_gap": 0})
    return [
        product(
            client, url=f"https://item.taobao.com/item.htm?id={8000+i}", paused=False
        )
        for i in range(count)
    ]


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


def test_five_parallel_one_stuck_does_not_block_remaining(client, monkeypatch):
    products = queued_products(client, 9)
    s.set_settings({"request_gap": 0, "max_retries": 0, "circuit_failures": 100})
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)

    async def exercise():
        release = asyncio.Event()
        first_five = asyncio.Event()
        started, active = set(), set()
        peak = 0
        stuck = products[0]["id"]

        async def crawl(p):
            nonlocal peak
            assert p["id"] not in started
            started.add(p["id"])
            active.add(p["id"])
            peak = max(peak, len(active))
            if len(started) == 5:
                first_five.set()
            try:
                if p["id"] == stuck:
                    await release.wait()
                else:
                    await first_five.wait()
                return {"price": None, "status": "price_missing", "stock": "unknown"}
            finally:
                active.remove(p["id"])

        monkeypatch.setattr(collector, "collect", crawl)
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 5
            health = d.health()
            assert len(health["running"]) == 5 and health["waiting"] == 4
            async with asyncio.timeout(5):
                while len(started) < 9:
                    pool.dispatch()
                    await asyncio.sleep(0.005)
            assert stuck in active and peak == 5
            assert len(pool.tasks) <= 5
            release.set()
            await asyncio.gather(*pool.tasks)
            pool.dispatch()
            assert d.health()["queued"] == 0
        finally:
            await pool.close()

    asyncio.run(exercise())


def test_timeout_is_terminal_even_when_retries_enabled(client, monkeypatch):
    products = queued_products(client, 2)
    s.set_settings(
        {
            "max_concurrency": 1,
            "request_gap": 0,
            "max_retries": 3,
            "circuit_failures": 100,
        }
    )
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(collector, "COLLECT_TIMEOUT_SECONDS", 0.04)
    calls, cancelled = [], []

    async def crawl(p):
        calls.append(p["id"])
        if p["id"] == products[0]["id"]:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(p["id"])
        return {"price": None, "status": "price_masked"}

    monkeypatch.setattr(collector, "collect", crawl)

    async def exercise():
        pool = worker.CollectionPool()
        try:
            async with asyncio.timeout(5):
                while d.health()["queued"]:
                    pool.dispatch()
                    await asyncio.sleep(0.005)
        finally:
            await pool.close()

    asyncio.run(exercise())
    assert calls == [p["id"] for p in products]
    assert cancelled == [products[0]["id"]]
    with s.connection() as db:
        row = dict(
            db.execute(
                "SELECT * FROM jobs WHERE product_id=?", (products[0]["id"],)
            ).fetchone()
        )
    result = json.loads(row["result"])
    assert row["status"] == "failed" and row["attempts"] == 1
    assert result["price"] is None and result["status"] == "timeout"
    assert result["diagnostics"]["timeout_seconds"] == 0.04


@pytest.mark.parametrize("before_start", [True, False])
def test_pool_shutdown_requeues_only_unfinished_jobs(client, monkeypatch, before_start):
    queued_products(client, 3)
    s.set_settings({"max_concurrency": 2})

    async def crawl(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(collector, "collect", crawl)

    async def exercise():
        pool = worker.CollectionPool()
        assert pool.dispatch() == 2
        if not before_start:
            await asyncio.sleep(0.01)
        await pool.close()
        assert not pool.tasks
        assert len(d.health()["running"]) == 0 and d.health()["waiting"] == 3

    asyncio.run(exercise())


def test_unexpected_exception_does_not_strand_other_jobs(client, monkeypatch):
    products = queued_products(client, 2)
    s.set_settings({"request_gap": 0, "max_retries": 0})
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)

    async def crawl(p):
        if p["id"] == products[0]["id"]:
            raise RuntimeError("isolated failure")
        return {"price": None, "status": "price_masked"}

    monkeypatch.setattr(collector, "collect", crawl)

    async def exercise():
        pool = worker.CollectionPool()
        pool.dispatch()
        await asyncio.gather(*pool.tasks)
        pool.dispatch()
        assert d.health()["queued"] == 0
        await pool.close()

    asyncio.run(exercise())
    with s.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM jobs WHERE status='failed'").fetchone()[0]
            == 2
        )


def test_concurrency_validation_and_pause(client):
    assert collector.COLLECT_TIMEOUT_SECONDS == 120
    assert s.settings()["max_concurrency"] == 5
    for invalid in (0, 6, True, 2.5, "5", None):
        assert (
            client.patch("/api/settings", json={"max_concurrency": invalid}).status_code
            == 422
        )
    assert client.patch("/api/settings", json={"max_concurrency": 2}).status_code == 200
    queued_products(client, 3)
    s.set_settings({"global_paused": True})
    assert worker.CollectionPool().dispatch() == 0
    s.set_settings({"global_paused": False})
    collector.LOGIN["status"] = "opening"
    try:
        assert worker.CollectionPool().dispatch() == 0
    finally:
        collector.LOGIN["status"] = "idle"


@pytest.mark.parametrize("paused", [False, True])
def test_manual_uncircuit_resets_failures_preserves_pause_and_session(client, paused):
    from datetime import timedelta

    s.set_settings(
        {
            "global_paused": paused,
            "failures": 8,
            "circuit_until": (d.utcnow() + timedelta(hours=1)).isoformat(),
        }
    )
    collector.LOGIN["status"] = "expired"
    try:
        response = client.post("/api/collection/uncircuit", json={})
        assert response.status_code == 200
        health = response.json()
        assert health["circuit_until"] is None and health["failures"] == 0
        assert health["paused"] is paused
        assert collector.LOGIN["status"] == "expired"
        with s.connection() as db:
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert s.settings()["circuit_failures"] == 5
    finally:
        collector.LOGIN["status"] = "idle"


def test_reader_cannot_uncircuit(client):
    token = "read.only"
    s.set_settings({"circuit_until": "2099-01-01T00:00:00+00:00", "failures": 5})
    response = client.post(
        "/api/collection/uncircuit",
        json={},
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 401
    assert s.settings()["circuit_until"] is not None
    assert s.settings()["failures"] == 5


def cookie(name, value):
    return {"name": name, "value": value, "domain": ".taobao.com", "path": "/"}


def test_parallel_cookie_merge_and_login_clear_protection(tmp_path):
    path = tmp_path / "session.json"
    session_state.save(
        path, {"cookies": [cookie("a", "old"), cookie("b", "old")], "origins": []}
    )
    generation, baseline = session_state.snapshot(path)
    session_state.merge(
        path,
        generation,
        baseline,
        {"cookies": [cookie("a", "first"), cookie("b", "old")], "origins": []},
    )
    session_state.merge(
        path,
        generation,
        baseline,
        {"cookies": [cookie("a", "conflict"), cookie("b", "second")], "origins": []},
    )
    assert {c["name"]: c["value"] for c in json.loads(path.read_text())["cookies"]} == {
        "a": "conflict",
        "b": "second",
    }
    session_state.save(path, {"cookies": [cookie("login", "new")], "origins": []})
    assert not session_state.merge(path, generation, baseline, baseline)
    assert json.loads(path.read_text())["cookies"][0]["name"] == "login"
    generation, baseline = session_state.snapshot(path)
    session_state.clear(path)
    assert not session_state.merge(path, generation, baseline, baseline)
    assert not path.exists()


def test_collectors_start_together_and_use_independent_cookie_files(
    client, monkeypatch
):
    session_state.save(
        collector.SESSION, {"cookies": [cookie("login", "saved")], "origins": []}
    )
    launched, files = [], []

    async def exercise():
        release = asyncio.Event()

        class FakeProcess:
            pid = 0
            returncode = 0

            def __init__(self, state, output, index):
                self.state, self.output, self.index = state, output, index

            async def communicate(self):
                await release.wait()
                state = json.loads(self.state.read_text())
                state["cookies"].append(cookie(f"task{self.index}", "refresh"))
                self.state.write_text(json.dumps(state))
                self.output.write_text(
                    json.dumps(
                        {
                            "price": "99.50",
                            "status": "success",
                            "session_state_saved": True,
                        }
                    )
                )
                return b"", b""

        async def launch(*args, **kwargs):
            assert kwargs["start_new_session"] is True
            state = Path(args[args.index("--state") + 1])
            assert json.loads(state.read_text())["cookies"][0]["value"] == "saved"
            assert state.stat().st_mode & 0o777 == 0o600
            output = Path(args[args.index("--output") + 1])
            files.append(state)
            process = FakeProcess(state, output, len(launched))
            launched.append(process)
            if len(launched) == 5:
                release.set()
            return process

        async def noop(*_):
            pass

        class FakeTree:
            def __init__(self, _):
                pass

            watch = noop

        monkeypatch.setattr(collector.asyncio, "create_subprocess_exec", launch)
        monkeypatch.setattr(collector, "stop_process", noop)
        monkeypatch.setattr(collector, "CrawlerProcessTree", FakeTree)
        async with asyncio.timeout(3):
            results = await asyncio.gather(
                *(
                    collector.collect(
                        {"url": f"https://item.taobao.com/item.htm?id={i}"}
                    )
                    for i in range(5)
                )
            )
        assert all(result["price"] == 9950 for result in results)

    asyncio.run(exercise())
    assert len(set(files)) == 5 and all(not path.exists() for path in files)
    state = json.loads(collector.SESSION.read_text())
    assert {c["name"] for c in state["cookies"]} == {
        "login",
        *(f"task{i}" for i in range(5)),
    }


@pytest.mark.skipif(os.name != "posix", reason="Mac/Linux process groups")
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_cancel_kill_detached_browser_descendants(
    client, monkeypatch, tmp_path, cancel
):
    """Use a real detached subprocess as Chrome's process-group stand-in."""
    marker = tmp_path / "child.pid"
    original_launch = asyncio.create_subprocess_exec
    launched = []
    script = (
        "import subprocess,sys,time,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True); "
        f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(120)"
    )

    async def launch(*args, **kwargs):
        process = await original_launch(sys.executable, "-c", script, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(collector.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(collector, "COLLECT_TIMEOUT_SECONDS", 0.7)

    def alive(pid):
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    async def exercise():
        task = asyncio.create_task(
            collector.collect({"url": "https://item.taobao.com/item.htm?id=123"})
        )
        try:
            await until(marker.exists)
            child = int(marker.read_text())
            assert os.getpgid(child) == child
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                result = await asyncio.wait_for(task, 3)
                assert result["status"] == "timeout" and result["price"] is None
            assert launched[0].returncode is not None
            await until(lambda: not alive(child))
            assert not list(s.DATA.glob(".crawl-*"))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            # Never leave the fixture's child alive if an assertion fails.
            if marker.exists():
                try:
                    psutil.Process(int(marker.read_text())).kill()
                except psutil.Error:
                    pass

    asyncio.run(exercise())
