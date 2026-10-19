"""Account credentials, tenant boundaries, files, and background task isolation."""

import asyncio
import hashlib
import io
import json
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import accounts, app as server, collector, domain as d, prediction, store as s, worker
from tests.test_system import client, product, record, TEST_PASSWORD


def register(browser, email="person@notreal.example", **extra):
    response = browser.post("/api/access/register", json={"email": email, "password": "UserPassword123", **extra})
    assert response.status_code == 200, response.text
    return browser.get("/api/state").json()["access"]["user"]


def login(browser, email, password="UserPassword123"):
    return browser.post("/api/access/login", json={"email": email, "password": password})


def test_local_gate_and_retired_credentials_never_bootstrap_admin(client):
    client.cookies.clear()
    assert "登录你的工作空间" in client.get("/").text
    assert client.get("/api/bootstrap").status_code == 401
    for token in ("legacy.admin", "read.only", "test-secret"):
        client.cookies.set("monitor_session", token)
        assert client.get("/api/state").status_code == 401
        assert client.get("/api/state", headers={"Authorization": "Bearer " + token}).status_code == 401
    response = login(client, accounts.ADMIN_EMAIL.lower(), TEST_PASSWORD)
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" not in response.headers["set-cookie"].split(",")[0]
    for route in ("/api/security/token", "/api/security/read-only-token"):
        assert client.get(route).status_code == 410
        assert client.post(route, json={}).status_code == 410


def test_registration_email_format_case_uniqueness_and_sole_admin(client):
    client.cookies.clear()
    for email in ("invalid", "x@localhost", "x@@example.com", "x@example..com"):
        assert client.post("/api/access/register", json={"email": email, "password": "UserPassword123"}).status_code == 422
    assert client.post("/api/access/register", json={"email": "ADMIN@EXAMPLE.COM", "password": "UserPassword123"}).status_code == 409
    user = register(client, role="admin", id="admin", tenant_id="admin", can_write=True)
    assert user["role"] == "user" and user["id"] != "admin"
    assert client.get("/api/state").json()["access"]["can_write"]
    assert product(client)["name"] == "测试显卡"
    assert client.get("/api/security/users").status_code == 403
    assert client.post("/api/access/register", json={"email": "PERSON@NOTREAL.EXAMPLE", "password": "UserPassword123"}).status_code == 409
    assert sum(user["role"] == "admin" for user in accounts.users()) == 1
    assert accounts.users()[0]["email"] == accounts.ADMIN_EMAIL


def test_products_prices_exports_settings_and_foreign_ids_are_isolated(client):
    admin_product = product(client, name="admin-only-product")
    record(admin_product, sku_verified=True)
    with TestClient(server.app) as first, TestClient(server.app) as second:
        user = register(first, "first@fake.example")
        other = register(second, "second@fake.example")
        own = product(first, name="first-only-product")
        assert first.get("/api/product/" + admin_product["id"]).status_code == 404
        assert first.put("/api/entities/product/" + admin_product["id"], json={"name": "hijacked"}).status_code == 404
        assert first.delete("/api/entities/product/" + admin_product["id"]).status_code == 404
        for path in ("crawl", "override", "daily-prices", "records"):
            assert second.post(f'/api/product/{own["id"]}/{path}', json={}).status_code == 404
        assert second.get("/api/state").json()["products"] == []
        assert first.get("/api/state").json()["products"][0]["name"] == "first-only-product"
        assert client.get("/api/state").json()["products"][0]["name"] == "admin-only-product"
        assert "admin-only-product" not in first.get("/api/export/products").text
        assert second.get("/api/export/observations").json() == []
        assert first.patch("/api/settings", json={"global_paused": True, "theme": "dark"}).status_code == 200
        assert second.get("/api/state").json()["settings"]["theme"] == "light"
        assert client.get("/api/state").json()["settings"]["theme"] == "light"
        assert first.post("/api/collection/all", json={}).status_code == 200
        assert len(first.get("/api/jobs").json()) == 1
        assert second.get("/api/jobs").json() == []
        assert user["id"] != other["id"]


def test_cookie_and_model_state_belong_to_one_account(client):
    admin_state = {"cookies": [{"name": "unb", "value": "admin-cookie", "domain": ".taobao.com"}], "origins": []}
    assert client.post("/api/session/import", json={"content": json.dumps(admin_state)}).status_code == 200
    before = collector.SESSION.read_bytes()
    with TestClient(server.app) as first, TestClient(server.app) as second:
        user = register(first, "cookie-first@fake.example")
        other = register(second, "cookie-second@fake.example")
        assert not first.get("/api/state").json()["session"]["exists"]
        assert first.post("/api/session/login", json={}).status_code == 403
        assert first.post("/api/session/import", json={"content": "unb=first-user-cookie"}).status_code == 200
        assert first.get("/api/state").json()["session"]["exists"]
        assert not second.get("/api/state").json()["session"]["exists"]
        assert collector.SESSION.read_bytes() == before
        roots = []
        for user_id in (user["id"], other["id"]):
            with s.tenant_scope(user_id):
                roots.append(prediction.root())
                assert prediction.runtime().TRAINING is None
        assert roots[0] != roots[1]
        assert first.post("/api/session/clear", json={}).status_code == 200
        assert collector.SESSION.read_bytes() == before


def test_backup_contains_only_current_user_workspace(client, tmp_path):
    product(client, name="admin-secret-product")
    with TestClient(server.app) as browser:
        register(browser)
        product(browser, name="user-own-product")
        filename = browser.post("/api/backup", json={}).json()["filename"]
        assert client.get("/api/backups/" + filename).status_code == 404
        response = browser.get("/api/backups/" + filename)
        assert response.status_code == 200
        backup = tmp_path / "downloaded.sqlite3"
        backup.write_bytes(response.content)
        db = sqlite3.connect(backup)
        rows = db.execute("SELECT data FROM entities WHERE kind='product'").fetchall()
        assert len(rows) == 1 and "user-own-product" in rows[0][0]
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone()
        db.close()
        assert b"admin-secret-product" not in response.content


def test_password_hash_sessions_revocation_and_admin_controls(client):
    with TestClient(server.app) as browser:
        user = register(browser)
        old_cookie = browser.cookies.get(accounts.COOKIE)
        with accounts.connection() as db:
            password = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()[0]
            stored = db.execute("SELECT digest FROM sessions WHERE user_id=?", (user["id"],)).fetchone()[0]
        assert "UserPassword123" not in password
        assert stored == hashlib.sha256(old_cookie.encode()).hexdigest() and stored != old_cookie
        assert browser.post("/api/account/password", json={"current_password": "wrong", "password": "ChangedUser123"}).status_code == 422
        assert browser.post("/api/account/password", json={"current_password": "UserPassword123", "password": "ChangedUser123"}).status_code == 200
        assert browser.get("/api/state").status_code == 401
        assert login(browser, user["email"], "ChangedUser123").status_code == 200
        assert client.post(f'/api/security/users/{user["id"]}/password', json={"password": "ResetUser123"}).status_code == 200
        assert browser.get("/api/state").status_code == 401
        assert login(browser, user["email"], "ResetUser123").status_code == 200
        assert client.patch(f'/api/security/users/{user["id"]}', json={"disabled": True}).status_code == 200
        assert browser.get("/api/state").status_code == 401
        assert login(browser, user["email"], "ResetUser123").status_code == 401
        assert client.patch(f'/api/security/users/{user["id"]}', json={"disabled": False}).status_code == 200
        assert login(browser, user["email"], "ResetUser123").status_code == 200
        assert client.patch('/api/security/users/admin', json={"disabled": True}).status_code == 403
        assert client.post('/api/security/users/admin/password', json={"password": "ResetUser123"}).status_code == 403


def test_logout_remember_and_credentials_changed_during_login(client):
    user, version = accounts.authenticate(accounts.ADMIN_EMAIL, TEST_PASSWORD)
    assert client.post("/api/account/password", json={"current_password": TEST_PASSWORD, "password": "NewAdminPass123"}).status_code == 200
    with pytest.raises(HTTPException):
        accounts.create_session(user["id"], version=version)
    response = client.post("/api/access/login", json={"email": accounts.ADMIN_EMAIL, "password": "NewAdminPass123", "remember": True})
    assert "Max-Age=2592000" in response.headers["set-cookie"]
    token = client.cookies.get(accounts.COOKIE)
    assert client.post("/api/access/logout", json={}).status_code == 200
    client.cookies.set(accounts.COOKIE, token)
    assert client.get("/api/state").status_code == 401


@pytest.mark.parametrize("limits", [(5, 5), (5, 2)])
def test_multiple_tenants_have_independent_collection_slots(client, monkeypatch, limits):
    people = [accounts.register(f'worker{i}@fake.example', "UserPassword123") for i in range(2)]
    for user_id, limit in zip(people, limits):
        with s.tenant_scope(user_id):
            s.set_settings({"crawler_start_gap": 0, "request_gap": 0, "max_retries": 0, "circuit_failures": 100, "ml_auto_train": False, "max_concurrency": limit})
            collector.session_state.save(collector.runtime().SESSION, {"cookies": [{"name": "unb", "value": user_id, "domain": ".taobao.com"}], "origins": []})
            for index in range(7):
                p = s.create("product", {"name": f'{user_id}-{index}', "url": f'https://item.taobao.com/item.htm?id={index+100}', "paused": False, "interval_hours": 24})
                s.enqueue(p["id"])
    monkeypatch.setattr(accounts, "active_tenants", lambda: people)
    monkeypatch.setattr(worker, "schedule", lambda: None)
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)

    async def exercise():
        active = 0
        peak = 0
        by_user = {user_id: 0 for user_id in people}
        user_peak = by_user.copy()
        first_wave = asyncio.Event()
        owners = []
        async def collect(p):
            nonlocal active, peak
            owner = s.TENANT.get()
            assert p["name"].startswith(owner)
            assert owner in str(collector.runtime().SESSION)
            active += 1
            peak = max(peak, active)
            by_user[owner] += 1
            user_peak[owner] = max(user_peak[owner], by_user[owner])
            if active == sum(limits):
                first_wave.set()
            try:
                await first_wave.wait()
                await asyncio.sleep(0.05)
                owners.append(owner)
                return {"price": None, "status": "price_missing", "stock": "unknown"}
            finally:
                active -= 1
                by_user[owner] -= 1
        async def idle():
            await asyncio.Event().wait()
        monkeypatch.setattr(collector, "collect", collect)
        monkeypatch.setattr(worker, "housekeeping", idle)
        task = asyncio.create_task(worker.run())
        try:
            async with asyncio.timeout(8):
                while len(owners) < 14:
                    await asyncio.sleep(0.02)
            assert peak == sum(limits)
            assert user_peak == dict(zip(people, limits))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for user_id in people:
            with s.tenant_scope(user_id):
                assert len(d.records()) == 7
                assert all(s.get(r["product_id"])["name"].startswith(user_id) for r in d.records())
        assert s.TENANT.get() == "admin"
    asyncio.run(exercise())


def test_fifty_shared_normal_slots_queue_resume_and_exclude_admin(client, monkeypatch):
    people = [accounts.register(f'capacity{i}@fake.example', "UserPassword123") for i in range(11)]
    owners = people + ["admin"]
    for owner in owners:
        with s.tenant_scope(owner):
            s.set_settings({"crawler_start_gap": 0, "request_gap": 0, "max_retries": 0, "circuit_failures": 100, "ml_auto_train": False})
            if owner != "admin":
                collector.session_state.save(collector.runtime().SESSION, {"cookies": [{"name": "unb", "value": "fixture-own", "domain": ".taobao.com"}], "origins": []})
            for index in range(6):
                p = s.create("product", {"name": f'{owner}-{index}', "url": f'https://item.taobao.com/item.htm?id={index+100}', "paused": False, "interval_hours": 24})
                s.enqueue(p["id"])
    monkeypatch.setattr(accounts, "active_tenants", lambda: owners)
    monkeypatch.setattr(worker, "schedule", lambda: None)
    monkeypatch.setattr(worker.random, "uniform", lambda *_: 0)

    async def exercise():
        active = {owner: 0 for owner in owners}
        peaks = active.copy()
        normal_peak = 0
        started = []
        releases = {}

        async def collect(p):
            nonlocal normal_peak
            owner = s.TENANT.get()
            assert p["name"].startswith(owner)
            key = (owner, p["id"])
            releases[key] = asyncio.Event()
            started.append(key)
            active[owner] += 1
            peaks[owner] = max(peaks[owner], active[owner])
            normal = sum(active[user] for user in people)
            normal_peak = max(normal_peak, normal)
            assert normal <= 50
            assert active[owner] <= 5
            try:
                await releases[key].wait()
                return {"price": None, "status": "price_masked", "stock": "unknown"}
            finally:
                active[owner] -= 1

        async def idle():
            await asyncio.Event().wait()

        async def until(predicate):
            async with asyncio.timeout(10):
                while not predicate():
                    await asyncio.sleep(0.01)

        monkeypatch.setattr(collector, "collect", collect)
        monkeypatch.setattr(worker, "housekeeping", idle)
        task = asyncio.create_task(worker.run())
        try:
            await until(lambda: sum(active[user] for user in people) == 50 and active["admin"] == 5)
            assert len(started) == 55
            waiting = 0
            for owner in people:
                with s.tenant_scope(owner), s.connection() as db:
                    waiting += db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
                    assert db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0] <= 5
                    assert db.execute("SELECT count(*) FROM jobs WHERE status IN ('success','failed')").fetchone()[0] == 0
            assert waiting == 16  # 66 ordinary tasks: 50 running, 16 still queued.
            first = next(key for key in started if key[0] != "admin")
            releases[first].set()
            await until(lambda: len(started) == 56)
            assert normal_peak == 50
            assert sum(active[user] for user in people) == 50
            assert active["admin"] == 5
            assert peaks["admin"] == 5
            assert all(value <= 5 for value in peaks.values())
            assert started[-1] != first
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert sum(active.values()) == 0
        for owner in owners:
            with s.tenant_scope(owner), s.connection() as db:
                assert db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 6
        assert s.TENANT.get() == "admin"

    asyncio.run(exercise())


def test_missing_user_cookie_waits_without_using_admin_session(client, monkeypatch):
    collector.session_state.save(collector.SESSION, {"cookies": [{"name": "unb", "value": "fixture-admin", "domain": ".taobao.com"}], "origins": []})
    admin_state = collector.SESSION.read_bytes()
    user_id = accounts.register("waiting-cookie@fake.example", "UserPassword123")
    async def forbidden_launch(*args, **kwargs):
        raise AssertionError("An account without its own cookie must not launch a crawler")
    monkeypatch.setattr(collector.asyncio, "create_subprocess_exec", forbidden_launch)
    with s.tenant_scope(user_id):
        p = s.create("product", {"name": "waiting", "url": "https://item.taobao.com/item.htm?id=123", "paused": False, "interval_hours": 24})
        job_id = s.enqueue(p["id"])
        assert not worker.permitted(s.settings())
        assert worker.CollectionPool().dispatch() == 0
        with s.connection() as db:
            assert db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "queued"
        result = asyncio.run(collector.collect(p))
        assert result["price"] is None
        assert result["diagnostics"]["cookie_required"] is True
        assert result["diagnostics"]["loaded_cookie_count"] == 0
        assert "尚未上传自己的淘宝 Cookie" in result["diagnostics"]["message"]
        collector.session_state.save(collector.runtime().SESSION, {"cookies": [{"name": "unb", "value": "fixture-own", "domain": ".taobao.com"}], "origins": []})
        assert worker.permitted(s.settings())
    assert collector.SESSION.read_bytes() == admin_state


def test_crawler_process_receives_only_current_account_cookie(client, monkeypatch):
    from pathlib import Path
    user_id = accounts.register("own-cookie@fake.example", "UserPassword123")
    for owner, value in (("admin", "fixture-admin"), (user_id, "fixture-user")):
        with s.tenant_scope(owner):
            collector.session_state.save(collector.runtime().SESSION, {"cookies": [{"name": "unb", "value": value, "domain": ".taobao.com"}], "origins": []})
    received = {}
    class Process:
        pid = 0
        returncode = 0
        def __init__(self, output):
            self.output = output
        async def communicate(self):
            self.output.write_text(json.dumps({"price": None, "status": "price_missing", "stock": "unknown"}))
            return b"", b""
    async def launch(*args, **kwargs):
        state_file = Path(args[args.index("--state") + 1])
        received[s.TENANT.get()] = json.loads(state_file.read_text())["cookies"][0]["value"]
        if s.TENANT.get() != "admin":
            assert str(state_file).startswith(str(s.data_dir()))
        return Process(Path(args[args.index("--output") + 1]))
    async def noop(*args):
        pass
    class Tree:
        def __init__(self, _):
            pass
        watch = noop
    monkeypatch.setattr(collector.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(collector, "CrawlerProcessTree", Tree)
    monkeypatch.setattr(collector, "stop_process", noop)
    async def exercise():
        async def crawl(owner):
            with s.tenant_scope(owner):
                await collector.collect({"url": "https://item.taobao.com/item.htm?id=123"})
        await asyncio.gather(crawl("admin"), crawl(user_id))
    asyncio.run(exercise())
    assert received == {"admin": "fixture-admin", user_id: "fixture-user"}
