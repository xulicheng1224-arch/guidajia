"""The newest accepted session is authoritative across roles and sources."""

import asyncio
import json
from pathlib import Path

import pytest

from backend import collector, session_state, store as s, worker
from tests.test_accounts import register
from tests.test_login import fake_browser, until
from tests.test_system import client, product


def state(value, **extra):
    return {"cookies": [{"name": "unb", "value": value, "domain": ".taobao.com", "path": "/"}], "origins": [], **extra}


def value(path):
    return json.loads(path.read_text())["cookies"][0]["value"]


@pytest.mark.parametrize("ordinary", [False, True])
def test_latest_upload_wins_regardless_of_export_time_and_survives_runtime_reset(client, ordinary):
    if ordinary:
        client.cookies.clear()
        owner = register(client)["id"]
    else:
        owner = "admin"
    with s.tenant_scope(owner):
        path = collector.runtime().SESSION
        collector.accept_session(state("server-login"), "登录已保存")
        generation, baseline, order = session_state.refresh_snapshot(path)
    for incoming in (
        state("upload-one", export_info={"exported_at": "2099-01-01T00:00:00Z"}),
        state("upload-two", export_info={"exported_at": "2000-01-01T00:00:00Z"}),
    ):
        assert client.post("/api/session/import", json={"content": json.dumps(incoming)}).status_code == 200
    assert value(path) == "upload-two"
    assert not session_state.merge(path, generation, baseline, state("old-crawl"), order=order)
    assert value(path) == "upload-two"
    before = path.read_bytes()
    if ordinary:
        collector._TENANTS.pop(str(path.parent.parent))
    else:
        collector.LOGIN.update(status="idle", message="尚未登录")
    with s.tenant_scope(owner):
        assert collector.session_info()["status"] == "saved"
        assert session_state.snapshot(collector.runtime().SESSION)[1]["cookies"][0]["value"] == "upload-two"
    assert path.read_bytes() == before
    assert "upload-two" not in client.get("/api/state").text
    assert path.stat().st_mode & 0o777 == 0o600


def test_upload_supersedes_open_mac_login_and_closes_old_window(client, fake_browser, monkeypatch):
    monkeypatch.setattr(collector, "login_task", None)
    collector.accept_session(state("before-login"), "已保存")
    fake_browser.context.state = state("older-window")
    assert client.post("/api/session/login").status_code == 200
    client.portal.call(until, lambda: collector.LOGIN["status"] == "waiting")
    response = client.post("/api/session/import", json={"content": json.dumps(state("new-upload"))})
    assert response.status_code == 200
    assert response.json()["status"] == "saved"
    assert fake_browser.close_count == 1 and collector.login_task.done()
    assert value(collector.SESSION) == "new-upload"
    assert client.post("/api/session/complete").status_code == 409


def test_new_login_can_replace_upload_but_old_login_cannot(fake_browser):
    collector.accept_session(state("original-upload"), "已保存")

    async def exercise():
        fake_browser.context.state = state("new-mac-login")
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        collector.login_done.set()
        await asyncio.wait_for(task, 1)
        assert value(collector.SESSION) == "new-mac-login"

    asyncio.run(exercise())


def test_login_started_before_a_new_session_cannot_replace_it(fake_browser):
    collector.accept_session(state("original"), "已保存")

    async def exercise():
        fake_browser.context.state = state("old-login-window")
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        collector.accept_session(state("newest"), "最新会话")
        collector.login_done.set()
        await asyncio.wait_for(task, 1)
        assert value(collector.SESSION) == "newest"
        assert collector.LOGIN["status"] == "saved" and collector.LOGIN["message"] == "最新会话"
        assert fake_browser.close_count == 1

    asyncio.run(exercise())


def test_invalid_upload_does_not_interrupt_login_or_replace_saved_cookie(client, fake_browser, monkeypatch):
    monkeypatch.setattr(collector, "login_task", None)
    collector.accept_session(state("saved"), "已保存")
    client.post("/api/session/login").raise_for_status()
    client.portal.call(until, lambda: collector.LOGIN["status"] == "waiting")
    try:
        assert client.post("/api/session/import", json={"content": "{broken"}).status_code == 422
        assert fake_browser.connected and value(collector.SESSION) == "saved"
    finally:
        client.post("/api/session/cancel").raise_for_status()


def test_clear_cancels_login_and_prevents_legacy_resurrection(client, fake_browser, monkeypatch):
    monkeypatch.setattr(collector, "login_task", None)
    collector.accept_session(state("saved"), "已保存")
    client.post("/api/session/login").raise_for_status()
    client.portal.call(until, lambda: collector.LOGIN["status"] == "waiting")
    assert client.post("/api/session/clear").status_code == 200
    assert fake_browser.close_count == 1
    assert not collector.SESSION.exists()
    assert not session_state.can_migrate(collector.SESSION)
    # Simulate a restart: the clear decision must not depend on process memory.
    session_state._GENERATION.pop(str(collector.SESSION), None)
    assert not session_state.can_migrate(collector.SESSION)
    client.post("/api/session/import", json={"content": "unb=new-after-clear"}).raise_for_status()
    assert value(collector.SESSION) == "new-after-clear"
    assert not collector.SESSION.with_suffix(".cleared").exists()


def test_newer_browser_refresh_wins_even_if_older_task_finishes_last(tmp_path):
    path = tmp_path / "taobao.json"
    session_state.save(path, state("initial"))
    old_generation, old_state, old_order = session_state.refresh_snapshot(path)
    generation, baseline, order = session_state.refresh_snapshot(path)
    assert session_state.merge(path, generation, baseline, state("newer-browser"), order=order)
    assert session_state.merge(path, old_generation, old_state, state("older-browser"), order=old_order)
    assert value(path) == "newer-browser"
    # Empty/partial browser state is not a replacement submitted by a user.
    generation, baseline, order = session_state.refresh_snapshot(path)
    unchanged = path.stat().st_mtime_ns
    session_state.merge(path, generation, baseline, {"cookies": [], "origins": []}, order=order)
    assert value(path) == "newer-browser" and path.stat().st_mtime_ns == unchanged


@pytest.mark.parametrize("ordinary", [False, True])
def test_old_task_failure_does_not_trip_new_cookie_circuit(client, monkeypatch, ordinary):
    owner = "admin"
    if ordinary:
        client.cookies.clear()
        owner = register(client)["id"]
    p = product(client, paused=False)
    with s.tenant_scope(owner):
        collector.accept_session(state("old"), "已保存")
        s.set_settings({"max_retries": 0, "circuit_failures": 1, "request_gap": 0})
        with s.connection() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE product_id=?", (p["id"],)).fetchone())

        async def collect(_):
            collector.accept_session(state("new"), "新会话")
            return {"price": None, "status": "login_or_redirect", "stock": "unknown"}

        monkeypatch.setattr(collector, "collect", collect)
        asyncio.run(worker.run_job(job))
        assert s.settings()["failures"] == 0 and s.settings()["circuit_until"] is None
        assert collector.LOGIN["status"] == "saved" if owner == "admin" else collector.runtime().LOGIN["status"] == "saved"
        assert value(collector.runtime().SESSION) == "new"


@pytest.mark.parametrize("result_status", ["success", "login_or_redirect"])
def test_finished_old_crawler_cannot_update_new_cookie_or_login_status(client, monkeypatch, result_status):
    collector.accept_session(state("old"), "旧会话")
    loaded = []

    async def exercise():
        class Process:
            pid = 0
            returncode = 0

            def __init__(self, session_file, output):
                self.session_file, self.output = session_file, output

            async def communicate(self):
                loaded.append(value(self.session_file))
                collector.accept_session(state("new"), "新会话")
                self.session_file.write_text(json.dumps(state("stale-refreshed")))
                self.output.write_text(json.dumps({"price": "99" if result_status == "success" else None,
                    "status": result_status, "session_state_saved": True}))
                return b"", b""

        async def launch(*args, **_):
            return Process(Path(args[args.index("--state") + 1]), Path(args[args.index("--output") + 1]))

        async def noop(*_):
            pass

        class Tree:
            def __init__(self, _):
                pass

            watch = noop

        monkeypatch.setattr(collector.asyncio, "create_subprocess_exec", launch)
        monkeypatch.setattr(collector, "CrawlerProcessTree", Tree)
        monkeypatch.setattr(collector, "stop_process", noop)
        result = await collector.collect({"url": "https://item.taobao.com/item.htm?id=123"})
        assert result["status"] == result_status

    asyncio.run(exercise())
    assert loaded == ["old"] and value(collector.SESSION) == "new"
    assert collector.LOGIN["status"] == "saved" and collector.LOGIN["message"] == "新会话"
