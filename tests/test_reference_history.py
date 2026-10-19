import asyncio
import csv
import io
import json
from datetime import timedelta

from backend import (
    domain as d,
    prediction as engine,
    prediction_data as pd,
    store as s,
    worker,
)
from tests.test_system import client


def reference(client, name="测试型号 8G"):
    response = client.post(
        "/api/entities/product",
        json={"name": name, "url": "", "model": "测试型号", "spec": "8G"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def history(client, p):
    rows = [
        {
            "product_id": p["id"],
            "observed_at": f"2026-01-{day:02}T00:00:00+08:00",
            "price": 10000 + day,
            "price_unit": "cent",
            "price_basis": "reference",
            "data_quality": "measured" if day % 2 else "carried_forward",
            "import_source": "原表.xlsx",
            "source_cell": f"Sheet1!B{day+1}",
            "source_note": "日级实测" if day % 2 else "沿用前值",
        }
        for day in range(1, 16)
    ]
    response = client.post(
        "/api/import/observations", json={"text": json.dumps(rows), "preview": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["new"] == 15, response.text
    return rows


def test_reference_products_have_no_fake_urls_and_cannot_crawl(client):
    assert client.post("/api/entities/product", json={}).status_code == 422
    p = reference(client)
    other = reference(client, "另一个型号")
    assert p["url"] == "" and p["paused"]
    assert (
        client.post(
            "/api/entities/product",
            json={"name": p["name"], "model": p["model"], "spec": p["spec"]},
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/api/entities/product/{p['id']}", json={"paused": False}
        ).status_code
        == 422
    )
    for action in ("resume", "crawl"):
        assert (
            client.post(
                "/api/products/bulk", json={"ids": [p["id"]], "action": action}
            ).status_code
            == 422
        )
    assert client.post(f"/api/product/{p['id']}/crawl").status_code == 409
    worker.schedule()
    assert client.get("/api/jobs").json() == []
    client.delete(f"/api/entities/product/{p['id']}").raise_for_status()
    assert client.post(f"/api/trash/{p['id']}/restore").status_code == 200
    assert len(s.entities("product")) == 2


def test_all_imported_reference_prices_participate_equally_in_training(client):
    p = reference(client)
    history(client, p)
    rows = d.records(p["id"])
    assert len(rows) == 15 and all(r["chart_price"] for r in rows)
    assert all(r["item_id"] is None and r["requested_sku_id"] is None for r in rows)
    pack = engine.read_pack(refresh=True)
    assert len(pack["series"]) == 1
    assert len(pack["series"][(p["id"], pd.reference_key(p["id"]))]) == 15
    assert sum(pd.usable(r, s.settings()) for r in rows) == 15
    assert all(pd.price_basis(r) == "reference" for r in rows)
    result = client.get(f"/api/product/{p['id']}").json()
    assert result["product"]["current_price"] == 10015
    assert result["product"]["has_reference_history"]
    exported = client.get(
        f"/api/export/observations?format=csv&product_id={p['id']}"
    ).text
    again = client.post(
        "/api/import/observations", json={"text": exported, "preview": False}
    ).json()
    assert again["new"] == 0 and again["skipped"] == 15, again


def test_adding_link_keeps_reference_history_separate_from_real_sku(client):
    p = reference(client)
    history(client, p)
    linked = client.put(
        f"/api/entities/product/{p['id']}",
        json={
            "url": "https://item.taobao.com/item.htm?id=123&skuId=456",
            "version": p["version"],
        },
    ).json()
    assert linked["id"] == p["id"] and linked["paused"]
    d.save_observation(
        p["id"],
        {
            "price": 20000,
            "stock": "in_stock",
            "status": "success",
            "sku_verified": True,
        },
    )
    pack = engine.read_pack(refresh=True)
    assert set(pack["series"]) == {
        (p["id"], pd.reference_key(p["id"])),
        (p["id"], "123:456:regular"),
    }
    assert len(d.records(p["id"])) == 16
    assert client.get(f"/api/product/{p['id']}").json()["product"]["lowest"] == 20000
    assert all(pd.matches_product(r, linked) for r in d.records(p["id"]))
    assert (
        client.get(f"/api/product/{p['id']}").json()["product"]["current_price"]
        == 20000
    )
    assert (
        len(
            client.get(f"/api/product/{p['id']}/daily-prices?basis=reference").json()[
                "rows"
            ]
        )
        == 15
    )


def test_daily_edit_confirms_reference_point_and_undo_restores_quality(client):
    p = reference(client)
    history(client, p)
    endpoint = f"/api/product/{p['id']}/daily-prices"
    days = client.get(endpoint + "?basis=reference").json()
    assert days["reference"] and days["item_id"] is None and days["sku_id"] is None
    response = client.post(
        endpoint,
        json={
            "day": "2026-01-02",
            "basis": "reference",
            "price": 111,
            "reason": "人工重新核实",
            "item_id": None,
            "sku_id": None,
        },
    )
    assert response.status_code == 200, response.text
    override = response.json()
    row = next(
        r
        for r in d.records(p["id"])
        if pd.day(r["observed_at"]).isoformat() == "2026-01-02"
    )
    assert row["price"] == 11100 and row["corrected"] and pd.usable(row, s.settings())
    client.post(
        f"/api/daily-prices/{override['id']}/undo",
        json={"version": override["version"]},
    ).raise_for_status()
    row = next(
        r
        for r in d.records(p["id"])
        if pd.day(r["observed_at"]).isoformat() == "2026-01-02"
    )
    assert row["price"] == 10002 and pd.usable(row, s.settings())
    assert (
        client.post(
            endpoint,
            json={
                "day": "2026-01-20",
                "basis": "reference",
                "price": 99,
                "reason": "真实补录",
                "item_id": None,
                "sku_id": None,
            },
        ).status_code
        == 200
    )
    virtual = next(r for r in d.records(p["id"]) if r.get("virtual_daily"))
    assert pd.identity(virtual) == pd.reference_key(p["id"]) and pd.usable(
        virtual, s.settings()
    )


def test_reference_identity_is_not_inferred_from_name_and_cannot_claim_taobao_sku(
    client,
):
    first = reference(client, "同型号 A")
    second = reference(client, "同型号 B")
    history(client, first)
    history(client, second)
    series = engine.read_pack(refresh=True)["series"]
    assert len(series) == 2
    row = d.records(first["id"])[0]
    assert not pd.matches_product(row, second)
    assert pd.identity({**row, "reference_product_id": second["id"]}) is None
    assert (
        pd.identity({**row, "source": "crawler", "manual": False, "corrected": False})
        is None
    )
    invalid = {**row, "item_id": "123", "price_unit": "cent"}
    response = client.post(
        "/api/import/observations", json={"text": json.dumps([invalid])}
    ).json()
    assert response["skipped"] == 1 and "参考价" in response["rows"][0]["error"]


def test_reference_csv_product_can_omit_url_column_and_training_reports_insufficient(
    client,
):
    result = client.post(
        "/api/import/product",
        json={"text": "name,model,spec\n参考型号,参考型号,8G", "preview": False},
    ).json()
    assert result["new"] == 1, result
    p = s.entities("product")[0]
    history(client, p)
    result = engine.train()
    assert engine.latest_run()["state"] == "insufficient"
    progress = engine.report()["progress"]
    assert progress["series"] == 1 and progress["daily_points"] == 15
    assert not engine.active_report()


def test_finished_training_during_status_read_keeps_ui_polling(client, monkeypatch):
    class CompletingTask:
        finished = False

        def done(self):
            return self.finished

    task = CompletingTask()
    monkeypatch.setattr(engine, "TRAINING", task)

    def read_run():
        task.finished = True
        return {"state": "running"}

    monkeypatch.setattr(engine, "latest_run", read_run)
    result = engine.report()
    assert result["last_run"]["state"] == "running" and result["running"]
