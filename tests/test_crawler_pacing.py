"""Account-local start spacing, challenge backoff and browser consistency."""

import argparse
import asyncio
import json

import pytest

import crawler
from backend import accounts, collector, crawl_pacing, domain as d, session_verification, store as s, worker
from browser_config import taobao_context_options
from tests.test_system import client, product


def cookie(value="fixture"):
    return {"cookies": [{"name": "unb", "value": value, "domain": ".taobao.com", "path": "/"}], "origins": []}


def jobs(count, client):
    return [product(client, paused=False, url=f"https://item.taobao.com/item.htm?id={9100+i}") for i in range(count)]


def test_staggered_starts_still_allow_five_parallel_pages(client, monkeypatch):
    jobs(5, client)
    s.set_settings({"crawler_start_gap": .025, "request_gap": 0, "max_retries": 0})
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)

    async def exercise():
        started, active, peak = [], 0, 0
        all_started = asyncio.Event()

        async def collect(_):
            nonlocal active, peak
            started.append(asyncio.get_running_loop().time())
            active += 1
            peak = max(peak, active)
            if len(started) == 5:
                all_started.set()
            try:
                await all_started.wait()
                return {"price": 12300, "status": "success", "stock": "in_stock"}
            finally:
                active -= 1

        monkeypatch.setattr(collector, "collect", collect)
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 5
            async with asyncio.timeout(3):
                await asyncio.gather(*pool.tasks)
            assert peak == 5
            assert all(b-a >= .022 for a,b in zip(started, started[1:]))
            with s.connection() as db:
                rows = db.execute("SELECT status,result,context FROM jobs").fetchall()
            assert all(r["status"] == "success" for r in rows)
            assert all("pacing" not in json.loads(r["context"]) for r in rows)
            assert max(json.loads(r["result"])["diagnostics"]["browser_start_wait_ms"] for r in rows) >= 80
        finally:
            await pool.close()

    asyncio.run(exercise())


def test_waiting_does_not_consume_browser_timeout(client, monkeypatch):
    jobs(2, client)
    s.set_settings({"crawler_start_gap": 2.1, "request_gap": 0, "max_retries": 0})
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(collector, "COLLECT_TIMEOUT_SECONDS", .02)

    async def collect(_):
        await asyncio.sleep(.002)
        return {"price": 10000, "status": "success", "stock": "in_stock"}

    monkeypatch.setattr(collector, "collect", collect)

    async def exercise():
        pool = worker.CollectionPool()
        try:
            pool.dispatch()
            async with asyncio.timeout(5):
                await asyncio.gather(*pool.tasks)
        finally:
            await pool.close()

    asyncio.run(exercise())
    with s.connection() as db:
        rows = db.execute("SELECT status,result FROM jobs").fetchall()
    assert all(r["status"] == "success" for r in rows)
    assert max(json.loads(r["result"])["diagnostics"]["browser_start_wait_ms"] for r in rows) >= 2000


def test_first_challenge_stops_retry_and_waiting_pages_and_persists(client, monkeypatch):
    jobs(4, client)
    collector.accept_session(cookie(), "已保存")
    before = collector.SESSION.read_bytes()
    s.set_settings({"crawler_start_gap": .04, "request_gap": 0, "max_retries": 3})
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)
    calls = []

    async def collect(p):
        calls.append(p["id"])
        await asyncio.sleep(.002)
        return {"price": None, "status": "blocked", "stock": "unknown"}

    monkeypatch.setattr(collector, "collect", collect)

    async def exercise():
        pool = worker.CollectionPool()
        try:
            pool.dispatch()
            await asyncio.gather(*pool.tasks)
        finally:
            await pool.close()

    asyncio.run(exercise())
    assert len(calls) == 1
    with s.connection() as db:
        rows = db.execute("SELECT status,attempts FROM jobs ORDER BY status").fetchall()
    assert sum(r["status"] == "failed" and r["attempts"] == 1 for r in rows) == 1
    assert sum(r["status"] == "queued" and r["attempts"] == 0 for r in rows) == 3
    assert collector.SESSION.read_bytes() == before
    assert session_verification.read(collector.SESSION)["state"] == "verification_required"
    restored = collector.initial_login(collector.SESSION)
    assert restored["status"] == "expired" and restored["issue"] == "verification_required"
    assert d.health()["queue"]["reason"] == "verification_required"
    collector.accept_session(cookie("updated"), "已更新")
    assert collector.initial_login(collector.SESSION)["status"] == "saved"


def test_start_spacing_is_isolated_by_account(client, monkeypatch):
    owner = accounts.register("pacing@fake.example", "FixtureUser123")
    monkeypatch.setattr(crawl_pacing.random, "uniform", lambda *_: 0)

    async def exercise():
        times = {}

        async def start(who, label):
            with s.tenant_scope(who):
                await crawl_pacing.wait_turn({"id": "fixture-only"}, .08, lambda: True)
                times[label] = asyncio.get_running_loop().time()

        await start("admin", "admin-first")
        await asyncio.gather(start("admin", "admin-second"), start(owner, "user-first"))
        assert times["admin-second"] - times["admin-first"] >= .075
        assert times["admin-second"] - times["user-first"] >= .05

    asyncio.run(exercise())


def test_cancelled_waiter_does_not_reserve_an_extra_turn(client, monkeypatch):
    p = jobs(1, client)[0]
    monkeypatch.setattr(crawl_pacing.random, "uniform", lambda *_: 0)
    with s.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE product_id=?", (p["id"],)).fetchone())
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))

    async def exercise():
        await crawl_pacing.wait_turn(job, .1, lambda: True)
        start = asyncio.get_running_loop().time()
        task = asyncio.create_task(crawl_pacing.wait_turn(job, .1, lambda: True))
        await asyncio.sleep(.01)
        response = client.get("/api/jobs").json()[0]
        assert response["pacing"]["resume_at"] and response["status"] == "running"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await crawl_pacing.wait_turn(job, .1, lambda: True)
        assert .09 <= asyncio.get_running_loop().time() - start < .18
        assert "pacing" not in client.get("/api/jobs").json()[0]

    asyncio.run(exercise())


def test_product_host_captcha_interrupts_render_without_exposing_tokens(tmp_path):
    args = argparse.Namespace(login=False, progress=tmp_path / "progress.json")
    result = {"status": "crawler_error", "diagnostics": {}}
    monitor = crawler.LoginRedirectMonitor(args, result)

    class Page:
        url = "https://detail.tmall.com/item.htm?id=123&token=SECRET"

        async def evaluate(self, _):
            return {"kind": "captcha", "signals": ["请完成验证"], "challenge_title": "验证码拦截"}

    async def exercise():
        await monitor.read_verification(Page())
        assert await asyncio.wait_for(monitor.wait_render(30), .1)

    asyncio.run(exercise())
    assert result["status"] == "blocked" and result["diagnostics"]["verification_required"]
    assert "SECRET" not in args.progress.read_text()


def test_pacing_settings_validate_and_default_to_safe_spacing(client):
    assert s.DEFAULTS["crawler_start_gap"] == 5
    for invalid in (0, 121, True, 1.5, "5", None):
        assert client.patch("/api/settings", json={"crawler_start_gap": invalid}).status_code == 422
    assert client.patch("/api/settings", json={"crawler_headed": "yes"}).status_code == 422
    assert client.patch("/api/settings", json={"crawler_start_gap": 7, "crawler_headed": True}).status_code == 200
    assert s.settings()["crawler_headed"] and s.settings()["crawler_start_gap"] == 7
    assert taobao_context_options() == {"locale": "zh-CN", "timezone_id": "Asia/Shanghai", "viewport": {"width": 1440, "height": 900}}
