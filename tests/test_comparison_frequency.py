"""Calendar comparison and global collection period behavior."""

from datetime import timedelta
import asyncio
import json
from pathlib import Path

from backend import collector, domain as d, prediction_data as pd, store as s
from tests.test_system import client, product


def save(p, price, observed_at, source="crawler", **extra):
    return d.save_observation(
        p["id"],
        {
            "price": price,
            "status": "success",
            "stock": "in_stock",
            "sku_verified": True,
            **extra,
        },
        source=source,
        observed_at=observed_at,
    )


def test_comparison_uses_shanghai_days_and_manual_precedence(client):
    p = product(
        client,
        paused=False,
        interval_hours=4,
        url="https://item.taobao.com/item.htm?id=123&skuId=101",
    )
    today = pd.day(d.utcnow())
    save(p, 10000, f"{today}T00:10:00+08:00")
    save(p, 12000, f"{today}T23:50:00+08:00", source="manual")
    save(p, 9000, f"{(today - timedelta(days=1)).isoformat()}T23:59:00+08:00")
    response = client.get(f"/api/product/{p['id']}/comparison-days")
    assert response.status_code == 200
    rows = response.json()["series"][0]["records"]
    assert [row["day"] for row in rows] == sorted(row["day"] for row in rows)
    current = next(row for row in rows if row["day"] == today.isoformat())
    assert current["price"] == 12000 and current["observed_at"].endswith("T00:00:00+08:00")


def test_frequency_updates_all_active_products_and_preserves_pause(client):
    active = product(
        client,
        paused=False,
        interval_hours=4,
        url="https://item.taobao.com/item.htm?id=123&skuId=101",
    )
    paused = product(
        client,
        paused=True,
        interval_hours=8,
        url="https://item.taobao.com/item.htm?id=124&skuId=102",
    )
    response = client.post("/api/collection/frequency", json={"interval_hours": 1.5})
    assert response.status_code == 200
    assert response.json()["updated"] == 2
    assert s.get(active["id"])["interval_hours"] == 1.5
    assert s.get(paused["id"])["paused"] is True
    assert s.get(paused["id"])["next_run"] is None


def test_frequency_rejects_invalid_values_and_reader_cannot_change_it(client):
    for value in (0, -1, 8761, True, "1"):
        assert client.post("/api/collection/frequency", json={"interval_hours": value}).status_code == 422
    token = "read.only"
    response = client.post(
        "/api/collection/frequency",
        json={"interval_hours": 1},
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 401


def test_collector_prioritizes_login_progress_over_deadline(client, monkeypatch):
    class FakeProcess:
        pid = 1
        returncode = None

        async def communicate(self):
            await asyncio.Event().wait()

    class FakeTree:
        closed = False

        def __init__(self, _pid):
            pass

        async def watch(self):
            await asyncio.Event().wait()

    async def launch(*args, **kwargs):
        progress = Path(args[args.index("--progress") + 1])
        progress.write_text(
            json.dumps(
                {
                    "status": "login_or_redirect",
                    "final_host": "login.taobao.com",
                    "diagnostics": {"stage": "login_redirect", "login_detected": True},
                }
            )
        )
        return FakeProcess()

    async def stop(*args, **kwargs):
        return None

    monkeypatch.setattr(collector.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(collector, "CrawlerProcessTree", FakeTree)
    monkeypatch.setattr(collector, "stop_process", stop)
    monkeypatch.setattr(collector, "COLLECT_TIMEOUT_SECONDS", 10)

    async def run():
        return await collector.collect({"url": "https://item.taobao.com/item.htm?id=321"})

    result = asyncio.run(run())
    assert result["status"] == "login_or_redirect"
    assert result["diagnostics"]["login_detected"] is True


def test_cookie_upload_accepts_storage_state_and_keeps_previous_backup(client):
    first = client.post(
        "/api/session/import",
        json={
            "filename": "cookies.json",
            "content": '{"cookies":[{"name":"unb","value":"first","domain":".taobao.com","path":"/"}]}',
        },
    )
    assert first.status_code == 200
    second = client.post(
        "/api/session/import",
        json={"content": "unb=second; cookie2=abc"},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "saved"
    assert second.json()["message"].startswith("已导入 2 条 Cookie")
    assert collector.SESSION.with_name("taobao.json.previous").is_file()
    assert '"value": "first"' in collector.SESSION.with_name("taobao.json.previous").read_text()


def test_cookie_upload_rejects_unrelated_domains(client):
    response = client.post(
        "/api/session/import",
        json={
            "content": '{"cookies":[{"name":"secret","value":"x","domain":".example.com","path":"/"}]}'
        },
    )
    assert response.status_code == 422
