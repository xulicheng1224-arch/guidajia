"""Public views retain price evidence while concealing deployment details."""

import json
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend import access, app as server, collector, domain as d, public_view, session_verification, store as s
from tests.test_accounts import register
from tests.test_system import client, product


def private_details():
    return {"stage": "price_region", "code": "price_missing", "message": "页面尚未提供完整价格",
            "browser": "msedge", "browser_headed": True, "framework": "Crawlee", "system": "Darwin",
            "hostname": "SecretHost.local", "cwd": "/Users/ServerSecret/Private Project",
            "python_version": "3.14.6", "pid": 1234,
            "exception_message": 'Permission denied: "/Users/ServerSecret/Private Project/data/state.json" on SecretHost.local at 192.168.50.17',
            "http_status": 200, "price_text": "￥379", "requested_sku_id": "321",
            "final_url": "https://item.taobao.com/item.htm?id=123&skuId=321",
            "attempts": [{"diagnostics": {"os_version": "Darwin", "path": r"C:\Users\ServerSecret\state.json"}}]}


def assert_private_removed(text):
    for private in ("ServerSecret", "Private Project", "SecretHost.local", "192.168.50.17", "Darwin", "服务器 Mac"):
        assert private not in text


def test_technical_view_removes_host_metadata_and_preserves_product_evidence():
    original = private_details()
    clean = public_view.technical(original)
    assert_private_removed(json.dumps(clean, ensure_ascii=False))
    assert not ({"browser", "browser_headed", "framework", "system", "hostname", "cwd", "python_version", "pid"} & clean.keys())
    assert clean["stage"] == "price_region" and clean["http_status"] == 200
    assert clean["requested_sku_id"] == "321" and clean["price_text"] == "￥379"
    assert clean["final_url"] == original["final_url"]
    assert public_view.technical({"platform": "taobao"})["platform"] == "taobao"
    assert public_view.technical({"platform": "Darwin"}) == {}
    assert original["hostname"] == "SecretHost.local"
    authored = {"product_name": "服务器 Mac Pro", "reason": "/Users/UserProvided/source.txt"}
    assert public_view.technical(authored) == authored


def test_old_logs_jobs_price_history_and_downloads_are_presented_safely(client):
    p = product(client, name="MacBook Pro", paused=True)
    note = s.create("note", {"product_id": p["id"], "text": "用户备注 /Users/UserProvided/device.txt"})
    details = private_details()
    job_id = s.uid()
    raw_result = s.dumps({"status": "price_missing", "price": None, "framework": "Crawlee", "headless": True, "diagnostics": details})
    with s.connection() as db:
        db.execute("INSERT INTO jobs(id,product_id,status,created_at,started_at,finished_at,error,result,context) VALUES(?,?,'failed',?,?,?,'price_missing',?,'{}')", (job_id, p["id"], s.now(), s.now(), s.now(), raw_result))
    d.save_observation(p["id"], {"status": "price_missing", "price": None, "diagnostics": details}, job_id)
    s.log('在服务器 Mac 打开登录窗口失败，文件 "/Users/ServerSecret/Private Project/state.json"', details=details)
    with s.connection() as db:
        original_log = dict(db.execute("SELECT * FROM logs ORDER BY created_at DESC LIMIT 1").fetchone())
        original_observations = db.execute("SELECT * FROM observations").fetchall()
    for endpoint in ("/api/state", "/api/logs", "/api/jobs", f"/api/product/{p['id']}",
                     "/api/export/logs?format=json", "/api/export/logs?format=csv",
                     "/api/export/observations?format=json", "/api/export/observations?format=csv"):
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint
        assert_private_removed(response.text)
    feed = client.get("/api/collection/results", params={"after": s.dumps(["", ""])})
    assert feed.status_code == 200 and len(feed.json()["items"]) == 1
    assert_private_removed(feed.text)
    public_job = client.get("/api/jobs").json()[0]
    assert "framework" not in json.loads(public_job["result"])
    assert public_job["diagnostics"]["http_status"] == 200
    detail = client.get(f"/api/product/{p['id']}").json()
    assert detail["product"]["name"] == "MacBook Pro"
    assert detail["notes"][0]["text"] == note["text"]
    with s.connection() as db:
        assert db.execute("SELECT result FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == raw_result
        assert dict(db.execute("SELECT * FROM logs WHERE id=?", (original_log["id"],)).fetchone()) == original_log
        assert db.execute("SELECT * FROM observations").fetchall() == original_observations


def test_session_status_hides_old_diagnostics_without_touching_cookie(client, monkeypatch):
    collector.accept_session({"cookies": [{"name": "unb", "value": "fixture-private", "domain": ".taobao.com"}], "origins": []}, "已保存")
    original = collector.SESSION.read_bytes()
    monkeypatch.setattr(collector, "LOGIN", {**collector.LOGIN, "message": "请在服务器 Mac 完成登录"})
    session_verification.write(collector.SESSION, {"state": "verification_required", "message": "请在运行采集的 Mac 浏览器完成验证", "diagnostics": private_details()})
    state = client.get("/api/state").json()
    assert_private_removed(json.dumps(state, ensure_ascii=False))
    assert "browser" not in state["session"]["verification"]["diagnostics"]
    assert collector.SESSION.read_bytes() == original
    assert "ServerSecret" in collector.SESSION.with_name("verification.json").read_text()


def test_runtime_controls_and_schema_are_admin_only(client):
    assert "browser" in client.get("/api/state").json()["settings"]
    assert client.get("/openapi.json").status_code == 200
    before = s.settings()["browser"]
    with TestClient(server.app) as browser:
        register(browser, "privacy-user@fake.example")
        settings = browser.get("/api/state").json()["settings"]
        assert "browser" not in settings and "crawler_headed" not in settings
        for body in ({"browser": "chrome"}, {"crawler_headed": True}):
            assert browser.patch("/api/settings", json=body).status_code == 403
        assert browser.patch("/api/settings", json={"crawler_start_gap": 7}).status_code == 200
        for endpoint in ("/api/docs", "/openapi.json", "/redoc"):
            assert browser.get(endpoint).status_code == 403
    assert s.settings()["browser"] == before


def test_health_check_and_response_headers_are_minimal(client, monkeypatch):
    client.cookies.clear()
    response = client.get("/api/ping")
    assert response.json() == {"ok": True}
    monkeypatch.setattr(access, "guard", lambda _: JSONResponse({"ok": True}, headers={"Server": "uvicorn", "X-Powered-By": "Python", "X-Runtime": "Darwin"}))
    response = client.get("/api/ping")
    assert not any(name in response.headers for name in ("server", "x-powered-by", "x-runtime"))


def test_http_error_details_never_echo_private_paths(client, monkeypatch):
    def fail():
        raise HTTPException(422, '无法读取文件 "/Users/ServerSecret/Private Project/state.json"')
    monkeypatch.setattr(server.worker, "backup", fail)
    response = client.post("/api/backup")
    assert response.status_code == 422
    assert_private_removed(response.text)
    assert "内部路径" in response.json()["detail"]


def test_existing_account_backup_download_uses_a_sanitized_copy(client, tmp_path):
    with TestClient(server.app) as browser:
        user = register(browser, "backup-privacy@fake.example")
        with s.tenant_scope(user["id"]):
            s.log("文件读取失败", details=private_details())
        name = browser.post("/api/backup").json()["filename"]
        source = s.DATA / "users" / user["id"] / "backups" / name
        original = source.read_bytes()
        assert b"ServerSecret" in original
        response = browser.get("/api/backups/" + name)
        assert response.status_code == 200
        assert b"ServerSecret" not in response.content and b"SecretHost.local" not in response.content
        saved = tmp_path / "public-backup.sqlite3"
        saved.write_bytes(response.content)
        with sqlite3.connect(saved) as db:
            details = json.loads(db.execute("SELECT details FROM logs WHERE details IS NOT NULL").fetchone()[0])
            assert details["stage"] == "price_region" and details["price_text"] == "￥379"
            assert not db.execute("SELECT 1 FROM settings WHERE key IN ('browser','crawler_headed')").fetchone()
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert source.read_bytes() == original
