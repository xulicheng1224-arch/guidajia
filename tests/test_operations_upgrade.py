import asyncio
import fcntl
import hashlib
import json
import sqlite3
import threading
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import accounts, app as server, collection_queue, collector, domain as d, full_backup, prediction, session_verification, store as s, training_queue, worker
from tests.test_system import client, product, record

BACKUP_PASSWORD = "FullBackupFixture123!"


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


def test_global_training_fifo_dedup_isolation_and_cancelled_waiter(client, monkeypatch):
    people = ["admin"] + [accounts.register(f'train{i}@fake.example', "FixtureUser123") for i in range(2)]
    entered = []
    release = threading.Event()
    counts = {"active": 0, "peak": 0}
    def train():
        counts["active"] += 1
        counts["peak"] = max(counts["peak"], counts["active"])
        entered.append(s.TENANT.get())
        try:
            if s.TENANT.get() == "admin":
                assert release.wait(8)
            assert s.TENANT.get() in people
            return {"state": "insufficient"}
        finally:
            counts["active"] -= 1
    monkeypatch.setattr(prediction, "train", train)
    async def exercise():
        tasks = []
        try:
            for person in people:
                with s.tenant_scope(person):
                    assert await prediction.schedule(force=True)
                    assert not await prediction.schedule(force=True)
                    tasks.append(prediction.runtime().TRAINING)
            await until(lambda: entered == ["admin"])
            with s.tenant_scope(people[1]):
                assert training_queue.status()["position"] == 1
                assert prediction.report()["queued"]
                assert people[2] not in json.dumps(training_queue.status())
            tasks[0].cancel()  # Cancelling the UI waiter cannot release the real training slot.
            await asyncio.gather(tasks[0], return_exceptions=True)
            await asyncio.sleep(0.05)
            assert entered == ["admin"]
            release.set()
            await asyncio.gather(*tasks[1:])
            assert entered == people and counts["peak"] == 1
            assert training_queue.status()["state"] == "idle"
        finally:
            release.set()
            await training_queue.shutdown()
    asyncio.run(exercise())


def test_training_runner_cancellation_holds_slot_until_thread_finishes(client, monkeypatch):
    user = accounts.register("canceltrain@fake.example", "FixtureUser123")
    release, entered = threading.Event(), []
    def train():
        entered.append(s.TENANT.get())
        if s.TENANT.get() == "admin":
            assert release.wait(8)
        return {"state": "insufficient"}
    monkeypatch.setattr(prediction, "train", train)
    async def exercise():
        await prediction.schedule(force=True)
        first = prediction.runtime().TRAINING
        with s.tenant_scope(user):
            await prediction.schedule(force=True)
            second = prediction.runtime().TRAINING
        await until(lambda: entered == ["admin"])
        runner = training_queue.coordinator().runner
        runner.cancel()
        await asyncio.sleep(0.05)
        assert not runner.done() and entered == ["admin"]
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
        training_queue.kick()
        await asyncio.gather(first, second)
        assert entered == ["admin", user]
        await training_queue.shutdown()
    asyncio.run(exercise())


def test_training_recovery_and_error_do_not_stall_following_requests(client, monkeypatch):
    user = accounts.register("recovertrain@fake.example", "FixtureUser123")
    first, _ = training_queue.enqueue("fixture")
    with s.tenant_scope(user):
        second, _ = training_queue.enqueue("fixture")
    with accounts.connection() as db:
        db.execute("UPDATE training_queue SET state='running' WHERE id=?", (first,))
    training_queue.recover()
    def train():
        if s.TENANT.get() == "admin":
            raise RuntimeError("fixture-private-error")
        return {"state": "insufficient"}
    monkeypatch.setattr(prediction, "train", train)
    async def exercise():
        waits = [asyncio.create_task(training_queue.wait(job)) for job in (first, second)]
        training_queue.kick()
        results = await asyncio.gather(*waits)
        assert results[0] == {"state": "error", "error_type": "RuntimeError"}
        assert results[1]["state"] == "insufficient"
        await training_queue.shutdown()
    asyncio.run(exercise())


def test_queue_reasons_cancel_and_tenant_boundaries(client):
    p = product(client, paused=False)
    s.set_settings({"global_paused": True})
    own = client.get("/api/jobs").json()[0]
    assert own["queue"]["reason"] == "paused"
    assert own["queue"]["position"] == 1
    assert client.post(f'/api/jobs/{own["id"]}/cancel', json={}).status_code == 200
    with s.connection() as db:
        assert db.execute("SELECT status,attempts FROM jobs WHERE id=?", (own["id"],)).fetchone()[:] == ("cancelled", 0)
    with TestClient(server.app) as browser:
        assert browser.post("/api/access/register", json={"email": "queue@fake.example", "password": "FixtureUser123"}).status_code == 200
        assert browser.post(f'/api/jobs/{own["id"]}/cancel', json={}).status_code == 404
        user = browser.get("/api/state").json()["access"]["user"]["id"]
        user_product = product(browser, paused=False)
        assert browser.get("/api/jobs").json()[0]["queue"]["reason"] == "cookie_missing"
        browser.post("/api/session/import", json={"content": "unb=fixture-own"}).raise_for_status()
        class Task:
            def done(self):
                return False
        class Pool:
            tasks = [Task() for _ in range(50)]
        collection_queue.publish({"other-owner": Pool()})
        try:
            state = browser.get("/api/state").json()
            assert state["health"]["queue"]["reason"] == "server_capacity"
            assert "other-owner" not in json.dumps(state["health"])
        finally:
            collection_queue.clear()
        with s.tenant_scope(user):
            s.set_settings({"circuit_until": (d.utcnow() + timedelta(hours=1)).isoformat()})
        assert browser.get("/api/state").json()["health"]["queue"]["reason"] == "circuit"


@pytest.mark.parametrize("status,expected", [("success", "passed"), ("login_or_redirect", "login_required"), ("blocked", "verification_required"), ("price_missing", "indeterminate")])
def test_cookie_real_check_uses_own_session_and_no_price_history(client, monkeypatch, status, expected):
    p = product(client, paused=True, name="admin-private-label")
    with TestClient(server.app) as browser:
        browser.post("/api/access/register", json={"email": "verify@fake.example", "password": "FixtureUser123"}).raise_for_status()
        user = browser.get("/api/state").json()["access"]["user"]["id"]
        assert browser.post("/api/session/verify", json={}).status_code == 409
        browser.post("/api/session/import", json={"content": "unb=fixture-own"}).raise_for_status()
        request = browser.post("/api/session/verify", json={})
        assert request.status_code == 200
        assert p["id"] not in request.text and p["name"] not in request.text
        calls = []
        async def collect(product):
            assert s.TENANT.get() == user
            assert json.loads(collector.runtime().SESSION.read_text())["cookies"][0]["value"] == "fixture-own"
            calls.append(product["url"])
            return {"price": 12345 if status == "success" else None, "status": status, "stock": "in_stock"}
        monkeypatch.setattr(collector, "collect", collect)
        monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)
        async def exercise():
            with s.tenant_scope(user):
                s.set_settings({"global_paused": True, "request_gap": 0, "max_retries": 3, "circuit_until": (d.utcnow() + timedelta(hours=1)).isoformat()})
                collector.runtime().LOGIN["status"] = "expired"
                pool = worker.CollectionPool()
                try:
                    assert pool.dispatch() == 1
                    await asyncio.gather(*pool.tasks)
                    assert d.records() == []
                    assert s.entities("product") == []
                    assert session_verification.read(collector.runtime().SESSION)["state"] == expected
                finally:
                    await pool.close()
        asyncio.run(exercise())
        assert calls == [p["url"]]
        assert "fixture-own" not in browser.get("/api/state").text
        browser.post("/api/session/import", json={"content": "unb=fixture-new"}).raise_for_status()
        assert browser.get("/api/state").json()["session"]["verification"]["state"] == "not_checked"


def test_old_inflight_result_cannot_verify_replaced_cookie(client):
    p = product(client, paused=False)
    path = collector.SESSION
    collector.session_state.save(path, {"cookies": [{"name": "unb", "value": "fixture-old", "domain": ".taobao.com"}], "origins": []})
    old_generation = collector.session_state.generation(path)
    with s.connection() as db:
        db.execute("UPDATE jobs SET status='running' WHERE product_id=?", (p["id"],))
    collector.session_state.save(path, {"cookies": [{"name": "unb", "value": "fixture-new", "domain": ".taobao.com"}], "origins": []})
    verification = session_verification.enqueue(p, path)
    assert session_verification.finish(path, verification["job_id"], {"status": "success", "price": 10000}, old_generation) is None
    assert session_verification.read(path)["state"] == "not_checked"


def test_cookie_verification_of_later_batch_can_run_while_auto_paused(client, monkeypatch):
    from backend import collection_batches
    values = [product(client, url=f'https://item.taobao.com/item.htm?id={1000+i}') for i in range(6)]
    collection_batches.enqueue_all()
    collector.session_state.save(collector.SESSION, {"cookies": [{"name": "unb", "value": "fixture", "domain": ".taobao.com"}], "origins": []})
    verification = session_verification.enqueue(values[-1], collector.SESSION)
    s.set_settings({"global_paused": True, "request_gap": 0})
    async def collect(p):
        return {"status": "success", "price": 10000, "stock": "in_stock", "sku_verified": True}
    monkeypatch.setattr(collector, "collect", collect)
    async def exercise():
        pool = worker.CollectionPool()
        try:
            assert pool.dispatch() == 1
            await asyncio.gather(*pool.tasks)
            assert session_verification.read(collector.SESSION)["state"] == "passed"
            with s.connection() as db:
                assert db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0] == 5
        finally:
            await pool.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("cookies", [False, True])
def test_encrypted_full_backup_restore_all_users_models_settings(client, tmp_path, cookies):
    admin_product = product(client)
    record(admin_product)
    user = accounts.register("backup@fake.example", "FixtureUser123")
    for owner in ("admin", user):
        with s.tenant_scope(owner):
            if owner != "admin":
                p = s.create("product", {"name": "user-private-product", "url": "", "paused": True})
                s.create("note", {"product_id": p["id"], "text": "user-private-note"})
            s.set_settings({"channels": {"email": {"password": "fixture-notification-secret"}}})
            collector.session_state.save(collector.runtime().SESSION, {"cookies": [{"name": "unb", "value": "fixture-cookie-" + owner, "domain": ".taobao.com"}], "origins": []})
            model = prediction.root() / ("a" * 32)
            model.mkdir()
            (model / "classifier.txt").write_text("fixture-model-" + owner)
    response = client.post("/api/security/full-backups", json={"password": BACKUP_PASSWORD, "include_cookies": cookies})
    assert response.status_code == 200, response.text
    name = response.json()["name"]
    source = s.DATA / "system-backups" / name
    assert source.stat().st_mode & 0o777 == 0o600
    assert b"fixture-notification-secret" not in source.read_bytes()
    assert full_backup.check(source, BACKUP_PASSWORD)["user_count"] == 2
    result = full_backup.restore(source, tmp_path / "recovered", BACKUP_PASSWORD)
    folder = tmp_path / "recovered"
    with sqlite3.connect(folder / "accounts.sqlite3") as db:
        assert {row[0] for row in db.execute("SELECT id FROM users")} == {"admin", user}
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    with sqlite3.connect(folder / "monitor.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
        assert json.loads(db.execute("SELECT value FROM settings WHERE key='channels'").fetchone()[0])["email"]["password"] == "fixture-notification-secret"
        assert db.execute("SELECT value FROM settings WHERE key='global_paused'").fetchone()[0] == "true"
    assert (folder / "users" / user / "forecast-models" / ("a" * 32) / "classifier.txt").read_text() == "fixture-model-" + user
    assert (folder / "auth" / "taobao.json").is_file() is cookies
    assert (folder / "users" / user / "auth" / "taobao.json").is_file() is cookies
    assert Path(result["rollback_directory"]).is_dir()


def test_full_backup_rejects_wrong_password_tamper_and_live_restore(client, tmp_path):
    name = full_backup.create(BACKUP_PASSWORD)["name"]
    source = s.DATA / "system-backups" / name
    folder = tmp_path / "unchanged"
    folder.mkdir()
    sentinel = folder / "original.txt"
    sentinel.write_text("original")
    for password in ("WrongPassword123",):
        with pytest.raises(ValueError, match="密码错误"):
            full_backup.restore(source, folder, password)
        assert sentinel.read_text() == "original"
    damaged = tmp_path / "damaged.gdbak"
    data = bytearray(source.read_bytes())
    data[-1] ^= 1
    damaged.write_bytes(data)
    with pytest.raises(ValueError):
        full_backup.restore(damaged, folder, BACKUP_PASSWORD)
    assert sentinel.read_text() == "original"
    with (folder / "service.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="正在运行"):
            full_backup.restore(source, folder, BACKUP_PASSWORD)
    with TestClient(server.app) as browser:
        browser.post("/api/access/register", json={"email": "no-backup@fake.example", "password": "FixtureUser123"}).raise_for_status()
        for method, path in (("GET", "/api/security/full-backups"), ("POST", "/api/security/full-backups"), ("GET", "/api/security/full-backups/" + name), ("POST", f"/api/security/full-backups/{name}/check")):
            assert browser.request(method, path, json={"password": BACKUP_PASSWORD}).status_code == 403


def test_full_restore_rejects_traversal_and_rolls_back_write_failure(client, tmp_path, monkeypatch):
    source = s.DATA / "system-backups" / full_backup.create(BACKUP_PASSWORD)["name"]
    clear = tmp_path / "clear.zip"
    full_backup.decrypt(source, clear, BACKUP_PASSWORD)
    with zipfile.ZipFile(clear, "a") as bundle:
        bundle.writestr("../escape.txt", "fixture")
    bad = tmp_path / "bad.gdbak"
    full_backup.encrypt(clear, bad, BACKUP_PASSWORD)
    with pytest.raises(ValueError):
        full_backup.restore(bad, tmp_path / "target", BACKUP_PASSWORD)
    assert not (tmp_path / "escape.txt").exists()
    folder = tmp_path / "rollback"
    folder.mkdir()
    (folder / "original.txt").write_text("original")
    original = Path.rename
    failed = []
    def rename(path, destination):
        if path.name == "accounts.sqlite3" and Path(destination).parent == folder and not failed:
            failed.append(True)
            raise OSError("fixture disk failure")
        return original(path, destination)
    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError):
        full_backup.restore(source, folder, BACKUP_PASSWORD)
    assert (folder / "original.txt").read_text() == "original"
    assert not (folder / "accounts.sqlite3").exists()
