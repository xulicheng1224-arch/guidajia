import argparse
import asyncio
import json
import sqlite3

import pytest

import crawler
from backend import collector, domain as d, store as s, worker
from diagnostics import exception_detail, product_url
from tests.test_system import client, product


@pytest.mark.parametrize(
    "status",
    [
        "price_region_missing",
        "blocked",
        "login_or_redirect",
        "price_masked",
        "setup_error",
        "timeout",
    ],
)
def test_sku_check_never_overwrites_underlying_failure(status):
    result = collector.finalize_result(
        {
            "status": status,
            "price": None,
            "requested_sku_id": "123",
            "sku_verified": False,
        },
        {},
    )
    assert result["status"] == status
    assert result["price"] is None
    assert result["diagnostics"]["code"] == status


def test_price_rejected_for_unknown_sku_but_evidence_retained():
    result = collector.finalize_result(
        {
            "status": "success",
            "price": "3929.36",
            "requested_sku_id": "123",
            "selected_options": ["8GB"],
            "sku_verified": False,
        },
        {},
    )
    assert result["status"] == "sku_unverified"
    assert result["price"] is None
    assert result["diagnostics"]["detected_price"] == "3929.36"
    assert result["diagnostics"]["stage"] == "sku_verification"


def test_verified_page_mapping_accepts_price_and_wrong_sku_cannot_be_overridden():
    raw = {
        "status": "success",
        "price": "3929.36",
        "requested_sku_id": "123",
        "selected_sku_id": "123",
        "selected_options": ["8GB"],
        "sku_verified": True,
        "sku_verification": "page_sku_mapping",
    }
    result = collector.finalize_result(raw, {})
    assert result["price"] == 392936 and result["status"] == "success"
    raw.update(selected_sku_id="456", sku_verified=False)
    result = collector.finalize_result(raw, {"expected_options": ["8GB"]})
    assert result["price"] is None and result["status"] == "sku_mismatch"


def test_error_details_do_not_expose_urls_headers_or_tokens():
    detail = exception_detail(
        RuntimeError(
            "net::ERR_CONNECTION_RESET at https://login.taobao.com/?token=SECRET Authorization: Bearer SECRET"
        ),
        "navigation",
    )
    assert detail["error_code"] == "ERR_CONNECTION_RESET"
    assert "SECRET" not in json.dumps(detail)
    assert (
        product_url("https://login.taobao.com/path?token=SECRET") == "login.taobao.com"
    )
    assert (
        product_url("https://detail.tmall.com/item.htm?id=123&skuId=456&token=SECRET")
        == "https://detail.tmall.com/item.htm?id=123&skuId=456"
    )


def test_constructor_failure_always_writes_diagnostic_result(tmp_path, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("Executable doesn't exist at /private/path/SECRET")

    monkeypatch.setattr(crawler, "PlaywrightCrawler", broken)
    args = argparse.Namespace(
        url="https://item.taobao.com/item.htm?id=123",
        login=False,
        headed=False,
        browser="msedge",
        wait=0,
        state=tmp_path / "state.json",
        output=tmp_path / "result.json",
    )
    assert asyncio.run(crawler.crawl(args)) == 1
    result = json.loads(args.output.read_text())
    assert result["status"] == "setup_error"
    assert result["diagnostics"]["stage"] == "browser_launch"
    assert "SECRET" not in args.output.read_text()


def test_details_persist_through_job_log_and_observation(client, monkeypatch):
    p = product(client, paused=False)
    s.set_settings({"max_retries": 0})

    async def fail(_):
        return {
            "price": None,
            "status": "price_region_missing",
            "stock": "unknown",
            "diagnostics": {
                "stage": "price_region",
                "http_status": 200,
                "price_nodes": 0,
            },
        }

    monkeypatch.setattr(collector, "collect", fail)
    with s.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs").fetchone())
    asyncio.run(worker.run_job(job))
    result = client.get("/api/jobs").json()[0]
    assert result["diagnostics"]["stage"] == "price_region"
    assert len(result["diagnostics"]["attempts"]) == 1
    log = client.get("/api/logs").json()[0]
    assert log["details"]["job_id"] == job["id"]
    assert log["details"]["http_status"] == 200
    detail = client.get(f'/api/product/{p["id"]}').json()
    assert detail["records"][0]["diagnostics"]["price_nodes"] == 0
    assert detail["product"]["last_diagnostics"]["code"] == "price_region_missing"


def test_existing_log_table_migrates_without_losing_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DB", tmp_path / "old.sqlite3")
    db = sqlite3.connect(s.DB)
    db.execute(
        "CREATE TABLE logs(id TEXT PRIMARY KEY, product_id TEXT, level TEXT, message TEXT, created_at TEXT, duration_ms INTEGER)"
    )
    db.execute(
        "INSERT INTO logs VALUES('old',NULL,'info','old message','2026-01-01',NULL)"
    )
    db.commit()
    db.close()
    s.init()
    s.log("new message", details={"stage": "navigation"})
    with s.connection() as db:
        assert (
            db.execute("SELECT message FROM logs WHERE id='old'").fetchone()[0]
            == "old message"
        )
        assert db.execute("SELECT count(*) FROM logs").fetchone()[0] == 2


def test_login_monitor_allows_sso_handoff_and_only_confirms_persistent_login(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(crawler.time, "monotonic", lambda: clock[0])
    args = argparse.Namespace(login=False, progress=tmp_path / "progress.json")
    result = {"status": "crawler_error", "diagnostics": {}}
    monitor = crawler.LoginRedirectMonitor(args, result)
    login = "https://login.taobao.com/havanaone/login/login.htm?token=SECRET"
    monitor.observe(login)
    clock[0] = 2.0
    monitor.observe(login)
    assert result["status"] != "login_or_redirect"
    monitor.observe("https://detail.tmall.com/item.htm?id=123")
    assert result["diagnostics"]["login_handoff_completed"]
    clock[0] = 20.0
    monitor.observe(login)
    clock[0] = 24.99
    monitor.observe(login)
    assert result["status"] != "login_or_redirect"
    clock[0] = 25.0
    monitor.observe(login)
    assert result["status"] == "login_or_redirect"
    assert result["diagnostics"]["login_confirmation"] == "login_host_persisted"
    assert "SECRET" not in args.progress.read_text()


def test_login_monitor_does_not_interrupt_interactive_login():
    args = argparse.Namespace(login=True)
    result = {"status": "crawler_error", "diagnostics": {}}
    crawler.LoginRedirectMonitor(args, result, grace_seconds=0).observe(
        "https://login.taobao.com/havanaone/login/login.htm"
    )
    assert result["status"] == "crawler_error"


def test_sms_verification_progress_stays_blocked_and_has_specific_message(tmp_path):
    args = argparse.Namespace(login=False, progress=tmp_path / "progress.json")
    result = {"status": "crawler_error", "diagnostics": {}}
    monitor = crawler.LoginRedirectMonitor(args, result, grace_seconds=0)

    class Page:
        url = "https://passport.taobao.com/iv/verify?token=SECRET"

        async def evaluate(self, _code):
            return {"kind": "sms", "signals": ["短信", "验证码", "异常"]}

    asyncio.run(monitor.read_verification(Page()))
    monitor.observe(Page.url)
    progress = json.loads(args.progress.read_text())
    assert "SECRET" not in args.progress.read_text()
    normalized = collector._progress_result(progress)
    assert normalized["status"] == "blocked" and normalized["price"] is None
    assert normalized["diagnostics"]["verification_kind"] == "sms"
    assert "短信" in normalized["diagnostics"]["message"]
    assert not normalized["diagnostics"]["login_detected"]


def test_sms_challenge_does_not_automatically_retry(client, monkeypatch):
    p = product(client, paused=False)
    s.set_settings({"max_retries": 3})
    calls = []

    async def fail(_):
        calls.append(1)
        return {"status": "blocked", "price": None,
                "diagnostics": {"verification_required": True, "verification_kind": "sms"}}

    monkeypatch.setattr(collector, "collect", fail)
    with s.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE product_id=?", (p["id"],)).fetchone())
    asyncio.run(worker.run_job(job))
    assert len(calls) == 1
    with s.connection() as db:
        row = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
    assert row["attempts"] == 1 and row["status"] == "failed"
