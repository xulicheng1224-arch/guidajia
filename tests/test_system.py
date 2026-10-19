import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend import accounts, app as server, collector, domain as d, store as s, worker

TEST_PASSWORD = "TestAdmin123"
from crawler import parse_price


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MONITOR_DISABLE_WORKER", "1")
    monkeypatch.setattr(s, "DATA", tmp_path)
    monkeypatch.setattr(s, "DB", tmp_path / "test.sqlite3")
    auth = tmp_path / "auth"
    auth.mkdir()
    monkeypatch.setattr(collector, "SESSION", auth / "taobao.json")
    token = tmp_path / "token"
    token.write_text("test-secret")
    monkeypatch.setattr(server, "TOKEN_FILE", token)
    accounts.provision_admin(TEST_PASSWORD)
    collector.LOGIN.update(status="idle", message="尚未登录", issue=None, navigating=False)
    with TestClient(server.app) as c:
        response = c.post("/api/access/login", json={"email": accounts.ADMIN_EMAIL, "password": TEST_PASSWORD})
        assert response.status_code == 200
        c.get("/api/bootstrap")
        yield c


def product(client, **kwargs):
    response = client.post(
        "/api/entities/product",
        json={
            "url": "https://item.taobao.com/item.htm?id=123",
            "name": "测试显卡",
            "paused": True,
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def record(p, price=10000, **kwargs):
    return d.save_observation(
        p["id"], {"price": price, "status": "success", "stock": "in_stock", **kwargs}
    )


def test_auth_and_cross_site(client):
    assert client.get("/api/state").status_code == 200
    assert (
        client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/state").status_code == 401
    assert (
        client.get(
            "/api/state", headers={"Authorization": "Bearer test-secret"}
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/item.htm?id=1",
        "http://item.taobao.com/item.htm?id=1",
        "https://item.taobao.com.evil.example/?id=1",
        "https://item.taobao.com:123/item.htm?id=1",
        "https://u:p@item.taobao.com/?id=1",
        "https://item.taobao.com/?id=abc",
        "https://item.taobao.com/?id=1&skuId=abc",
    ],
)
def test_reject_unsupported_urls(client, url):
    assert client.post("/api/entities/product", json={"url": url}).status_code == 422


def test_url_canonical_duplicate(client):
    p = product(
        client, url="https://detail.tmall.com/item.htm?id=123&skuId=456&spm=tracking"
    )
    assert p["url"] == "https://detail.tmall.com/item.htm?id=123&skuId=456"
    assert (
        client.post("/api/entities/product", json={"url": p["url"]}).status_code == 409
    )


@pytest.mark.parametrize(
    "text,price,masked",
    [
        ("到手价 ¥ 4999.00 优惠前 ¥ 6999", "4999.00", False),
        ("平台加补后 ¥ 4??? 优惠前 ¥ 6999", None, True),
        ("¥ 199.90", "199.90", False),
        ("优惠前 ¥ 6999", None, False),
        ("¥ 199 ~ ¥ 299", None, False),
        ("券后价 ￥ 299.99", "299.99", False),
        ("到手价 ¥ 4,999.99", "4999.99", False),
        ("到手价 ¥ 4999 . 99", "4999.99", False),
        ("到手价 ¥ 4,99", None, False),
    ],
)
def test_price_parser(text, price, masked):
    result = parse_price(text)
    assert result["sale_price"] == price
    assert result["masked"] == masked


def test_failure_none_does_not_replace_good_price(client):
    p = product(client)
    record(p, 10000)
    record(p, 0, status="crawler_error")
    result = client.get("/api/state").json()["products"][0]
    assert result["price"] is None
    assert result["current_price"] == 10000
    assert result["stale"]
    assert d.records(p["id"])[-1]["price"] is None


def test_correction_overlay_preserves_raw_and_undo(client):
    p = product(client)
    rid = record(p, 10000)
    assert (
        client.post(
            f"/api/records/{rid}/correct",
            json={"price": 90, "valid": True, "reason": "页面核对"},
        ).status_code
        == 200
    )
    row = d.records(p["id"])[0]
    assert row["price"] == 9000 and row["raw_price"] == 10000
    assert client.post(f"/api/records/{rid}/undo", json={}).status_code == 200
    assert d.records(p["id"])[0]["price"] == 10000


def test_missing_price_cannot_become_zero(client):
    p = product(client)
    assert (
        client.post(f'/api/product/{p["id"]}/records', json={"price": 0}).status_code
        == 422
    )
    assert (
        client.post(
            f'/api/product/{p["id"]}/records', json={"price": "nan"}
        ).status_code
        == 422
    )
    assert collector.cents("0.29") == 29


def test_optimistic_version_conflict(client):
    p = product(client)
    path = f'/api/entities/product/{p["id"]}'
    assert (
        client.put(
            path, json={"name": "第一次修改", "version": p["version"]}
        ).status_code
        == 200
    )
    assert (
        client.put(path, json={"name": "过期修改", "version": p["version"]}).status_code
        == 409
    )
    assert s.get(p["id"])["name"] == "第一次修改"


def test_soft_delete_restore_starts_paused(client):
    p = product(client)
    rid = record(p)
    assert client.delete(f'/api/entities/product/{p["id"]}').status_code == 200
    assert client.get("/api/state").json()["products"] == []
    assert len(client.get("/api/trash").json()) == 1
    assert client.post(f'/api/trash/{p["id"]}/restore', json={}).json()["paused"]
    assert d.records(p["id"])[0]["id"] == rid


def test_purge_requires_soft_delete(client):
    p = product(client)
    assert client.delete(f'/api/trash/{p["id"]}').status_code == 409
    record(p)
    client.delete(f'/api/entities/product/{p["id"]}')
    assert client.delete(f'/api/trash/{p["id"]}').status_code == 200
    assert not d.records(p["id"])


def test_audit_rollback_and_conflict(client):
    p = product(client)
    path = f'/api/entities/product/{p["id"]}'
    client.put(path, json={"name": "修改一"})
    change = next(r for r in client.get("/api/audit").json() if r["action"] == "update")
    client.put(path, json={"name": "修改二"})
    assert (
        client.post(f'/api/audit/{change["id"]}/rollback', json={}).status_code == 409
    )
    assert (
        client.post(
            f'/api/audit/{change["id"]}/rollback', json={"force": True}
        ).status_code
        == 200
    )
    assert s.get(p["id"])["name"] == "测试显卡"
    assert (
        client.post(f'/api/audit/{change["id"]}/rollback', json={}).status_code == 409
    )


def test_manual_price_rollback_no_nested_transaction(client):
    p = product(client)
    response = client.post(f'/api/product/{p["id"]}/records', json={"price": 99})
    assert response.status_code == 200
    audit = next(r for r in client.get("/api/audit").json() if r["kind"] == "manual")
    assert client.post(f'/api/audit/{audit["id"]}/rollback', json={}).status_code == 200
    assert d.records(p["id"])[0]["valid"] is False


def test_queue_dedup_and_idempotent_result(client):
    p = product(client)
    job = s.enqueue(p["id"])
    assert s.enqueue(p["id"]) == job
    first = d.save_observation(
        p["id"], {"price": 10000, "status": "success", "stock": "in_stock"}, job_id=job
    )
    assert (
        d.save_observation(
            p["id"],
            {"price": 9900, "status": "success", "stock": "in_stock"},
            job_id=job,
        )
        == first
    )
    assert len(d.records(p["id"])) == 1


def test_anomaly_quarantine(client):
    p = product(client)
    record(p, 10000)
    record(p, 1000)
    rows = d.records(p["id"])
    assert rows[-1]["status"] == "anomaly"
    assert not rows[-1]["valid"]
    assert client.get("/api/state").json()["products"][0]["current_price"] == 10000


def test_coupon_scope(client):
    p = product(client)
    record(p, 10000, price_type="coupon")
    assert client.get("/api/state").json()["products"][0]["current_price"] is None
    assert (
        client.patch("/api/settings", json={"include_coupon": True}).status_code == 200
    )
    assert client.get("/api/state").json()["products"][0]["current_price"] == 10000


def test_confirmed_change_failure_breaks_streak(client):
    p = product(client)
    record(p, 10000)
    record(p, 9000)
    record(p, None, status="timeout")
    record(p, 9000)
    assert d.changes(d.records(p["id"])) == []
    record(p, 9000)
    assert len(d.changes(d.records(p["id"]))) == 1


def test_rule_confirmations_and_no_duplicate_notifications(client):
    p = product(client)
    rule = client.post(
        "/api/entities/rule",
        json={
            "name": "目标价",
            "scope": "product",
            "scope_value": p["id"],
            "condition": "target",
            "value": 100,
            "confirmations": 2,
            "channels": ["inapp"],
        },
    )
    assert rule.status_code == 200
    record(p, 9900)
    d.evaluate_rules(p, d.records(p["id"]))
    assert not client.get("/api/deliveries").json()
    record(p, 9900)
    d.evaluate_rules(p, d.records(p["id"]))
    assert len(client.get("/api/deliveries").json()) == 1
    record(p, 9900)
    d.evaluate_rules(p, d.records(p["id"]))
    assert len(client.get("/api/deliveries").json()) == 1


def test_rules_ignore_failed_observation(client):
    p = product(client)
    client.post(
        "/api/entities/rule", json={"name": "目标价", "value": 200, "confirmations": 1}
    )
    record(p, 10000)
    record(p, None, status="timeout")
    d.evaluate_rules(p, d.records(p["id"]))
    assert not client.get("/api/deliveries").json()


def test_invalid_bulk_is_non_destructive(client):
    p = product(client)
    assert (
        client.post(
            "/api/products/bulk", json={"ids": [p["id"], "missing"], "action": "delete"}
        ).status_code
        == 404
    )
    assert s.get(p["id"])
    assert (
        client.post(
            "/api/products/bulk",
            json={"ids": [p["id"]], "action": "interval", "value": -1},
        ).status_code
        == 422
    )


def test_import_preview_no_write_and_skip_duplicates(client):
    text = json.dumps(
        [
            {"url": "https://item.taobao.com/item.htm?id=123"},
            {"url": "https://item.taobao.com/item.htm?id=123"},
            {"url": "https://bad.example/?id=1"},
        ]
    )
    preview = client.post(
        "/api/import/product", json={"text": text, "preview": True}
    ).json()
    assert preview["new"] == 1 and preview["skipped"] == 2
    assert not s.entities("product")
    assert (
        client.post(
            "/api/import/product", json={"text": text, "preview": False}
        ).status_code
        == 200
    )
    assert len(s.entities("product")) == 1 and s.entities("product")[0]["paused"]


def test_observation_roundtrip_import_cents(client):
    p = product(client)
    stamp = (d.utcnow() - timedelta(days=1)).isoformat()
    text = json.dumps(
        [
            {
                "product_id": p["id"],
                "price": 12345,
                "price_unit": "cent",
                "observed_at": stamp,
            }
        ]
    )
    response = client.post(
        "/api/import/observations", json={"text": text, "preview": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["new"] == 1
    assert d.records(p["id"])[0]["price"] == 12345
    assert (
        client.post(
            "/api/import/observations", json={"text": text, "preview": True}
        ).json()["skipped"]
        == 1
    )


def test_notification_secrets_are_redacted_and_backup_sanitized(client):
    assert (
        client.patch(
            "/api/settings",
            json={
                "channels": {
                    "bark": {"enabled": True, "url": "https://push.example/SECRET"}
                }
            },
        ).status_code
        == 200
    )
    state = client.get("/api/state").text
    assert "SECRET" not in state
    assert (
        client.patch(
            "/api/settings", json={"channels": {"bark": {"url": "__saved__"}}}
        ).status_code
        == 200
    )
    assert s.settings()["channels"]["bark"]["url"].endswith("SECRET")
    name = client.post("/api/backup", json={}).json()["filename"]
    db = sqlite3.connect(s.DATA / "backups" / name)
    assert (
        db.execute("SELECT value FROM settings WHERE key='channels'").fetchone()[0]
        == "{}"
    )
    assert client.get("/api/backups/" + name).status_code == 200
    db.close()


def test_event_validation_known_time_and_generation(client):
    assert (
        client.post(
            "/api/entities/event",
            json={"name": "倒序", "start": "2026-02-02", "end": "2026-01-01"},
        ).status_code
        == 422
    )
    response = client.post(
        "/api/entities/event",
        json={
            "name": "活动",
            "start": "2026-11-01",
            "end": "2026-11-11",
            "known_at": "2000-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 200
    assert response.json()["known_at"].startswith(str(d.utcnow().year))
    assert client.post("/api/events/generate", json={"year": 2027}).json()["count"] == 3
    assert client.post("/api/events/generate", json={"year": 2027}).json()["count"] == 0


def test_forecast_does_not_manufacture_confidence(client):
    p = product(client)
    record(p)
    prediction = client.get(f'/api/product/{p["id"]}').json()["product"]["forecast"]
    assert prediction["label"] == "数据不足"
    assert prediction["probability"] is None and prediction["interval"] is None
    assert client.post("/api/forecasts/recalculate", json={}).status_code == 200
    assert client.get("/api/forecast-review").status_code == 200


def test_settings_validation_and_token_rotation(client):
    assert client.patch("/api/settings", json={"max_retries": -1}).status_code == 422
    assert client.patch("/api/settings", json={"max_retries": 1.2}).status_code == 422
    assert (
        client.patch("/api/settings", json={"quiet_start": "99:99"}).status_code == 422
    )
    assert client.post("/api/security/token", json={}).status_code == 410
    assert client.get("/api/state").status_code == 200
    assert (
        client.get(
            "/api/state", headers={"Authorization": "Bearer test-secret"}
        ).status_code
        == 401
    )


def test_worker_failure_returns_none(client, monkeypatch):
    p = product(client, paused=False)
    s.set_settings({"max_retries": 0})

    async def fail(_):
        return {"price": None, "status": "timeout", "stock": "unknown"}

    monkeypatch.setattr(collector, "collect", fail)
    with s.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs").fetchone())
    asyncio.run(worker.run_job(job))
    result = client.get("/api/jobs").json()[0]
    assert result["status"] == "failed"
    assert json.loads(result["result"])["price"] is None


def test_circuit_breaker(client, monkeypatch):
    p = product(client, paused=False)
    s.set_settings({"max_retries": 0, "circuit_failures": 1})

    async def fail(_):
        return {"price": None, "status": "blocked", "stock": "unknown"}

    monkeypatch.setattr(collector, "collect", fail)
    with s.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs").fetchone())
    asyncio.run(worker.run_job(job))
    assert s.settings()["circuit_until"]
    assert client.post("/api/collection/probe", json={}).status_code == 200
    assert s.settings()["circuit_until"]  # Manual probing never resumes timers.


def test_csv_formula_escape(client):
    product(client, name='=HYPERLINK("https://bad.example")')
    text = client.get("/api/export/product?format=csv").text
    assert "'=HYPERLINK" in text


def test_reject_malformed_view_and_settings(client):
    assert (
        client.post(
            "/api/entities/view",
            json={"name": "坏视图", "config": {"dashboard_cards": "watch"}},
        ).status_code
        == 422
    )
    assert (
        client.patch("/api/settings", json={"card_sizes": {"watch": 99}}).status_code
        == 422
    )
    assert client.patch("/api/settings", json={"density": "bad"}).status_code == 422


def test_backup_restore_maintains_data(client, tmp_path):
    from restore_backup import restore

    p = product(client)
    record(p)
    name = worker.backup()
    target = tmp_path / "restore-target"
    restore(s.DATA / "backups" / name, target)
    restored = sqlite3.connect(target / "monitor.sqlite3")
    assert restored.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored.close()
