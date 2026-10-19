import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend import (
    collection_batches as batches,
    collector,
    domain as d,
    store as s,
    worker,
)
from tests.test_collection_pool import until
from tests.test_system import client, product


def products(client, count, **kwargs):
    return [
        product(client, url=f"https://item.taobao.com/item.htm?id={9000+i}", **kwargs)
        for i in range(count)
    ]


def start(client):
    s.set_settings({"crawler_start_gap": 0})
    response = client.post("/api/collection/all", json={})
    assert response.status_code == 200, response.text
    return response.json()


def jobs():
    with s.connection() as db:
        return [
            dict(row)
            for row in db.execute(f"SELECT * FROM jobs ORDER BY {batches.QUEUE_ORDER}")
        ]


def clock(monkeypatch):
    at = [datetime(2026, 10, 19, tzinfo=timezone.utc)]
    monkeypatch.setattr(s, "now", lambda: at[0].isoformat())
    monkeypatch.setattr(d, "utcnow", lambda: at[0])
    return at


def test_all_includes_paused_skips_invalid_missing_deleted_and_deduplicates(client):
    values = products(client, 3)
    product(client, url="", name="参考历史")
    invalid = product(client, url="https://item.taobao.com/item.htm?id=9990")
    s.update(invalid["id"], {"url": "https://example.com/invalid"})
    deleted = product(client, url="https://item.taobao.com/item.htm?id=9991")
    s.update(deleted["id"], {}, deleted_at=s.now())
    existing = s.enqueue(values[0]["id"])
    first = start(client)
    assert first["added"] == 2 and first["reused"] == 1
    assert first["skipped_no_url"] == 1 and first["skipped_invalid_url"] == 1
    assert first["batch"]["total"] == 3
    assert len(jobs()) == 3
    assert existing in {job["id"] for job in jobs()}
    assert all(s.get(p["id"])["paused"] for p in values)
    assert all(batches.context(job)["allow_paused"] for job in jobs())
    second = start(client)
    assert second["already_active"] and second["added"] == 0
    assert second["batch"]["id"] == first["batch"]["id"]
    assert len(jobs()) == 3


def test_running_task_not_duplicated_or_reassigned(client):
    values = products(client, 2, paused=False)
    rows = jobs()
    with s.connection() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (rows[0]["id"],))
    result = start(client)
    assert result["existing"] == 1 and result["reused"] == 1 and result["added"] == 0
    assert len(jobs()) == 2
    assert (
        batches.context(next(row for row in jobs() if row["status"] == "running")) == {"manual": 1}
    )


def test_exact_five_then_eight_second_gap_then_five_then_one(client, monkeypatch):
    at = clock(monkeypatch)
    values = products(client, 11)
    s.set_settings({"request_gap": 300, "max_retries": 0, "circuit_failures": 100})
    start(client)
    assert [batches.context(row)["wave"] for row in jobs()] == [0] * 5 + [1] * 5 + [2]

    async def exercise():
        release = asyncio.Event()
        first_five = asyncio.Event()
        started, active = [], set()
        peak = 0

        async def crawl(p):
            nonlocal peak
            assert p["id"] not in started
            started.append(p["id"])
            active.add(p["id"])
            peak = max(peak, len(active))
            position = len(started)
            if position == 5:
                first_five.set()
            try:
                if position == 1:
                    await release.wait()
                elif position <= 5:
                    await first_five.wait()
                return {"price": None, "status": "price_masked"}
            finally:
                active.remove(p["id"])

        monkeypatch.setattr(collector, "collect", crawl)
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 5
            await until(lambda: len(started) == 5 and len(active) == 1)
            at[0] += timedelta(seconds=30)
            assert pool.dispatch() == 0  # No next wave while one request is held.
            assert len(started) == 5
            release.set()
            await asyncio.gather(*pool.tasks)
            assert all(row["status"] == "failed" for row in jobs()[:5])
            assert d.health()["batch"]["completed"] == 5
            assert d.health()["batch"]["next_batch_at"]
            assert pool.dispatch() == 0
            at[0] += timedelta(seconds=7.999)
            assert pool.dispatch() == 0
            at[0] += timedelta(milliseconds=1)
            assert pool.dispatch() == 5
            await asyncio.gather(*pool.tasks)
            assert len(started) == 10
            assert pool.dispatch() == 0
            at[0] += timedelta(seconds=8)
            assert pool.dispatch() == 1
            await asyncio.gather(*pool.tasks)
            assert peak == 5 and len(started) == 11
            assert d.health()["queued"] == 0 and d.health()["batch"] is None
            assert all(s.get(p["id"])["paused"] for p in values)
        finally:
            await pool.close()

    asyncio.run(exercise())


def test_batch_timeout_finishes_once_and_allows_next_wave(client, monkeypatch):
    at = clock(monkeypatch)
    products(client, 6)
    s.set_settings({"max_retries": 3, "circuit_failures": 100})
    start(client)
    first_id = jobs()[0]["product_id"]
    monkeypatch.setattr(collector, "COLLECT_TIMEOUT_SECONDS", 0.03)
    calls = []

    async def crawl(p):
        calls.append(p["id"])
        if p["id"] == first_id:
            await asyncio.Event().wait()
        return {"price": None, "status": "price_masked"}

    monkeypatch.setattr(collector, "collect", crawl)

    async def exercise():
        pool = worker.CollectionPool()
        try:
            pool.dispatch()
            await asyncio.gather(*pool.tasks)
            failed = next(row for row in jobs() if row["product_id"] == first_id)
            assert (
                failed["attempts"] == 1
                and json.loads(failed["result"])["status"] == "timeout"
            )
            assert pool.dispatch() == 0
            at[0] += timedelta(seconds=8)
            assert pool.dispatch() == 1
            await asyncio.gather(*pool.tasks)
            assert len(calls) == 6 and len(set(calls)) == 6
        finally:
            await pool.close()

    asyncio.run(exercise())


def test_restart_preserves_wave_cooldown(client, monkeypatch):
    at = clock(monkeypatch)
    products(client, 6)
    start(client)
    first = worker.claim_jobs(5)
    with s.connection() as db:
        for job in first:
            db.execute(
                "UPDATE jobs SET status='failed',finished_at=?,context=json_set(context,'$.released_at',?) WHERE id=?",
                (s.now(), s.now(), job["id"]),
            )
    # A fresh pool has no in-memory wave state.
    assert worker.claim_jobs(5) == []
    at[0] += timedelta(seconds=7.999)
    assert worker.claim_jobs(5) == []
    at[0] += timedelta(milliseconds=1)
    assert len(worker.claim_jobs(5)) == 1


@pytest.mark.parametrize("cancel_remaining", [False, True])
def test_finished_batch_does_not_delay_new_manual_command(client, monkeypatch, cancel_remaining):
    clock(monkeypatch)
    values = products(client, 6 if cancel_remaining else 1)
    s.set_settings({"request_gap": 300, "max_retries": 0})
    start(client)

    async def crawl(_):
        return {"price": None, "status": "price_masked"}

    monkeypatch.setattr(collector, "collect", crawl)

    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == (5 if cancel_remaining else 1)
            await asyncio.gather(*pool.tasks)
            if cancel_remaining:
                with s.connection() as db:
                    db.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='queued'", (s.now(),))
            response = client.post(f"/api/product/{values[0]['id']}/crawl", json={})
            assert response.status_code == 200, response.text
            assert pool.dispatch() == 1  # no eight-second delay after the final wave
            await asyncio.gather(*pool.tasks)
        finally:
            await pool.close()

    asyncio.run(exercise())


def test_timer_pause_keeps_manual_permission_but_delete_cancels(client, monkeypatch):
    products(client, 2)
    start(client)
    row = jobs()[0]
    p = s.get(row["product_id"])
    assert batches.permitted_product(row, p)
    s.update(p["id"], {"paused": False})
    s.update(p["id"], {"paused": True})
    assert batches.permitted_product(row, s.get(p["id"]))
    s.update(p["id"], {}, deleted_at=s.now())
    assert not batches.permitted_product(row, s.get(p["id"]))
    asyncio.run(worker.run_job(row))
    assert (
        next(job for job in jobs() if job["id"] == row["id"])["status"] == "cancelled"
    )


@pytest.mark.parametrize("block", ["circuit", "login"])
def test_all_bypasses_historical_cookie_and_circuit_pauses(client, monkeypatch, block):
    products(client, 2)
    if block == "circuit":
        s.set_settings({"circuit_until": "2099-01-01T00:00:00+00:00"})
    else:
        collector.LOGIN["status"] = "expired"
    async def collect(_):
        return {"price":None,"status":"price_masked"}
    monkeypatch.setattr(collector,"collect",collect)
    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 2
            await asyncio.gather(*pool.tasks)
            assert all(job["status"] == "failed" for job in jobs())
        finally:
            await pool.close()
    try:
        assert start(client)["added"] == 2
        asyncio.run(exercise())
    finally:
        collector.LOGIN["status"] = "idle"


def test_reader_cannot_enqueue_all(client):
    products(client, 2)
    token = "read.only"
    assert (
        client.post(
            "/api/collection/all", json={}, headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )
    assert jobs() == []


def test_old_database_gets_context_without_losing_jobs(client, tmp_path, monkeypatch):
    old = tmp_path / "old.sqlite3"
    with sqlite3.connect(old) as db:
        db.execute(
            "CREATE TABLE jobs(id TEXT PRIMARY KEY,product_id TEXT,status TEXT,created_at TEXT,started_at TEXT,finished_at TEXT,attempts INTEGER DEFAULT 0,error TEXT,result TEXT)"
        )
        db.execute(
            "INSERT INTO jobs(id,product_id,status,created_at) VALUES('old','product','queued','2026-10-18T00:00:00+00:00')"
        )
    monkeypatch.setattr(s, "DB", old)
    s.init()
    s.init()
    assert jobs()[0]["id"] == "old" and jobs()[0]["context"] == "{}"


def test_empty_library_is_a_noop(client):
    result = start(client)
    assert result["added"] == result["existing"] == result["reused"] == 0
    assert result["batch"] is None


def test_batches_respect_lower_configured_concurrency(client, monkeypatch):
    clock(monkeypatch)
    products(client, 5)
    s.set_settings({"max_concurrency": 2})
    assert start(client)["batch"]["batch_size"] == 2
    assert [batches.context(row)["wave"] for row in jobs()] == [0, 0, 1, 1, 2]
    assert len(worker.claim_jobs(2)) == 2
    assert worker.claim_jobs(2) == []
