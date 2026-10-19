"""Confirmed daily prices participate in training without rewriting raw data."""

from datetime import timedelta

import pytest

from backend import domain as d, prediction as engine, prediction_data as pd, store as s
from tests.test_system import client, product


def record(p, price=10000, observed_at=None, **values):
    return d.save_observation(
        p["id"],
        {"price": price, "status": "success", "stock": "in_stock", **values},
        observed_at=observed_at,
    )


def tracked_product(client):
    return product(client, url="https://item.taobao.com/item.htm?id=123&skuId=101")


def yesterday():
    return (pd.day(d.utcnow()) - timedelta(days=1)).isoformat()


def endpoint(p):
    return f'/api/product/{p["id"]}/daily-prices'


def save(client, p, **values):
    return client.post(
        endpoint(p),
        json={
            "day": yesterday(),
            "basis": "regular",
            "price": 88.88,
            "reason": "核对订单",
            "item_id": "123",
            "sku_id": "101",
            "version": 0,
            **values,
        },
    )


def test_daily_edit_covers_all_visits_and_later_crawls_without_touching_raw(client):
    p = tracked_product(client)
    stamp = yesterday() + "T08:00:00+08:00"
    rid = record(p, 10000, observed_at=stamp, sku_verified=True)
    record(p, 11000, observed_at=yesterday() + "T12:00:00+08:00", sku_verified=True)
    revision = engine.correction_revision()
    edited = save(client, p)
    assert edited.status_code == 200, edited.text
    assert engine.correction_revision() != revision
    record(p, 12000, observed_at=yesterday() + "T14:00:00+08:00", sku_verified=True)
    rows = d.records(p["id"])
    assert len(rows) == 3
    assert {r["price"] for r in rows} == {8888}
    assert {r["raw_price"] for r in rows} == {10000, 11000, 12000}
    assert all(r["corrected"] and not r.get("exclude_prediction") for r in rows)
    assert all(pd.usable(r, s.settings()) for r in rows)
    bars = pd.daily_series(rows, s.settings(), d.utcnow())
    assert len(bars) == 1
    assert next(iter(bars.values()))[0]["minimum"] == 8888
    with s.connection() as db:
        assert (
            db.execute("SELECT price FROM observations WHERE id=?", (rid,)).fetchone()[
                0
            ]
            == 10000
        )
    daily = client.get(endpoint(p)).json()["rows"][0]
    assert daily["day"] == yesterday() and daily["price"] == 8888
    assert daily["raw_price"] == 12000 and daily["observations"] == 3
    undo = client.post(
        f'/api/daily-prices/{edited.json()["id"]}/undo',
        json={"version": edited.json()["version"]},
    )
    assert undo.status_code == 200
    assert [r["price"] for r in d.records(p["id"])] == [10000, 11000, 12000]
    assert all(pd.usable(r, s.settings()) for r in d.records(p["id"]))


def test_empty_day_is_reversible_virtual_manual_price_not_fake_observation(client):
    p = tracked_product(client)
    edited = save(client, p).json()
    rows = d.records(p["id"])
    assert len(rows) == 1 and rows[0]["manual"] and rows[0]["virtual_daily"]
    assert pd.day(rows[0]["observed_at"]).isoformat() == yesterday()
    assert rows[0]["raw_price"] is None and rows[0]["chart_price"] == 8888
    assert pd.usable(rows[0], s.settings())
    assert client.get(f'/api/product/{p["id"]}').status_code == 200
    with s.connection() as db:
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert (
        client.post(
            f'/api/daily-prices/{edited["id"]}/undo',
            json={"version": edited["version"]},
        ).status_code
        == 200
    )
    assert not d.records(p["id"])
    assert client.get(endpoint(p)).json()["rows"] == []


def test_daily_scope_does_not_cross_sku_price_basis_product_or_beijing_day(client):
    p = tracked_product(client)
    day = yesterday()
    before = (pd.day(d.utcnow()) - timedelta(days=2)).isoformat()
    # UTC 16:00 is next day's 00:00 in Shanghai.
    a = record(p, 10000, observed_at=before + "T16:00:00+00:00", sku_verified=True)
    b = record(p, 12000, observed_at=before + "T15:59:59+00:00", sku_verified=True)
    c = record(
        p,
        13000,
        observed_at=day + "T12:00:00+08:00",
        requested_sku_id="202",
        sku_verified=True,
    )
    e = record(
        p,
        14000,
        observed_at=day + "T12:00:00+08:00",
        price_basis="subsidy",
        sku_verified=True,
    )
    p2 = product(client, url="https://item.taobao.com/item.htm?id=222&skuId=101")
    record(p2, 15000, observed_at=day + "T12:00:00+08:00", sku_verified=True)
    assert save(client, p).status_code == 200
    rows = {r["id"]: r for r in d.records(p["id"])}
    assert rows[a]["price"] == 8888
    assert [rows[r]["price"] for r in (b, c, e)] == [12000, 13000, 14000]
    assert d.records(p2["id"])[0]["price"] == 15000
    assert (
        client.get(endpoint(p) + "?basis=subsidy").json()["rows"][0]["price"] == 14000
    )


@pytest.mark.parametrize(
    "values",
    [
        {"day": "2026-02-30"},
        {"day": None},
        {"day": "20260101"},
        {"day": "2099-01-01"},
        {"price": 0},
        {"price": -1},
        {"price": "NaN"},
        {"reason": "  "},
        {"reason": None},
        {"basis": "unknown"},
        {"basis": []},
        {"version": True},
    ],
)
def test_invalid_daily_edit_is_rejected_atomically(client, values):
    p = tracked_product(client)
    assert save(client, p, **values).status_code == 422
    assert not s.entities("daily_price")
    assert not d.records(p["id"])


def test_daily_update_optimistic_lock_undo_and_recreate(client):
    p = tracked_product(client)
    first = save(client, p).json()
    assert save(client, p, price=99).status_code == 409
    assert save(client, p, sku_id="202").status_code == 409
    update = save(client, p, price=99, version=first["version"]).json()
    assert update["version"] == first["version"] + 1
    assert d.records(p["id"])[0]["price"] == 9900
    assert (
        client.post(
            f'/api/daily-prices/{first["id"]}/undo', json={"version": first["version"]}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f'/api/daily-prices/{first["id"]}/undo', json={"version": update["version"]}
        ).status_code
        == 200
    )
    assert save(client, p).status_code == 200
    assert len(s.entities("daily_price")) == 1


def test_daily_undo_restores_existing_point_correction_and_audit_can_rollback(client):
    p = tracked_product(client)
    rid = record(
        p, 10000, observed_at=yesterday() + "T12:00:00+08:00", sku_verified=True
    )
    client.post(f"/api/records/{rid}/correct", json={"price": 95, "reason": "单点修正"})
    edited = save(client, p).json()
    assert d.records(p["id"])[0]["price"] == 8888
    client.post(
        f'/api/daily-prices/{edited["id"]}/undo', json={"version": edited["version"]}
    )
    assert d.records(p["id"])[0]["price"] == 9500
    audit = next(
        a
        for a in client.get("/api/audit").json()
        if a["kind"] == "daily_price" and a["action"] == "delete"
    )
    assert client.post(f'/api/audit/{audit["id"]}/rollback', json={}).status_code == 200
    assert d.records(p["id"])[0]["price"] == 8888


def test_daily_price_works_with_observed_sku_on_untargeted_link(client):
    p = product(client)
    assert save(client, p).status_code == 422
    record(
        p,
        10000,
        observed_at=yesterday() + "T12:00:00+08:00",
        selected_sku_id="101",
        sku_verified=True,
    )
    assert save(client, p).status_code == 200
    assert d.records(p["id"])[0]["price"] == 8888


def test_latest_manual_day_does_not_predict_using_an_older_price(client):
    p = tracked_product(client)
    today = pd.day(d.utcnow()).isoformat()
    record(p, 10000, observed_at=yesterday() + "T12:00:00+08:00", sku_verified=True)
    assert save(client, p, day=today).status_code == 200
    forecast = client.get(f'/api/product/{p["id"]}').json()["product"]["forecast"]
    assert forecast["current_price"] == 8888
    assert forecast["probability"] is None and forecast["interval"] is None
    assert forecast["model_status"] == "collecting"
    assert "人工" not in forecast["model_reason"]


def test_confirmed_daily_price_is_used_as_real_anomaly_baseline(client):
    p = tracked_product(client)
    record(p, 10000, observed_at=yesterday() + "T12:00:00+08:00", sku_verified=True)
    assert save(client, p, price=1).status_code == 200
    # A human confirmed one-yuan price is the real history. A later price of
    # 100 yuan therefore needs confirmation as an unusually large change.
    rid = record(p, 10000, sku_verified=True)
    row = next(r for r in d.records(p["id"]) if r["id"] == rid)
    assert row["status"] == "anomaly" and not row["valid"]


def test_daily_price_is_purged_with_product(client):
    p = tracked_product(client)
    edited = save(client, p).json()
    s.update(p["id"], {}, deleted_at=s.now())
    s.permanent_delete(p["id"])
    assert s.get(edited["id"], include_deleted=True) is None
    assert not d.records(p["id"])


def test_manual_backfill_produces_mature_samples_and_real_future_labels(client):
    p = tracked_product(client)
    start = pd.day(d.utcnow()) - timedelta(days=45)
    overrides = []
    for i in range(45):
        response = save(
            client, p, day=(start + timedelta(days=i)).isoformat(), price=100
        )
        assert response.status_code == 200
        overrides.append(response.json())
    pack = engine.read_pack(refresh=True)
    config = s.settings()
    samples = pd.training_samples(pack["series"], [], config, d.utcnow())
    assert len(samples) > 10
    assert all(sample["target"] == 0 for sample in samples)
    bars = next(iter(pack["series"].values()))
    assert len(bars) == 45
    origin = start + timedelta(days=10)
    assert pd.future_outcome(bars, origin, 14, d.utcnow())["minimum"] == 10000
    # Correcting an existing day changes the real minimum and labels, but does
    # not add another daily point. Undo removes the backfilled day entirely.
    response = save(
        client, p, day=overrides[15]["day"], price=50, version=overrides[15]["version"]
    )
    assert response.status_code == 200
    updated = response.json()
    pack = engine.read_pack(refresh=True)
    bars = next(iter(pack["series"].values()))
    assert len(bars) == 45
    assert pd.future_outcome(bars, origin, 14, d.utcnow())["minimum"] == 5000
    samples = pd.training_samples(pack["series"], [], config, d.utcnow())
    assert any(sample["target"] == 1 for sample in samples)
    client.post(
        f'/api/daily-prices/{updated["id"]}/undo', json={"version": updated["version"]}
    )
    bars = next(iter(engine.read_pack(refresh=True)["series"].values()))
    assert len(bars) == 44
    assert pd.future_outcome(bars, origin, 14, d.utcnow())["minimum"] == 10000


def test_manual_record_and_corrected_failed_crawl_are_trusted_price_data(client):
    p = tracked_product(client)
    response = client.post(
        f'/api/product/{p["id"]}/records', json={"price": 100, "reason": "真实订单价"}
    )
    assert response.status_code == 200
    assert pd.usable(d.records(p["id"])[0], s.settings())
    failed = d.save_observation(
        p["id"],
        {
            "status": "sku_mismatch",
            "price": None,
            "stock": "in_stock",
            "requested_sku_id": "101",
            "selected_sku_id": "202",
            "sku_verified": False,
        },
    )
    assert (
        client.post(
            f"/api/records/{failed}/correct",
            json={"price": 95, "valid": True, "reason": "人工核对目标规格价格"},
        ).status_code
        == 200
    )
    row = next(r for r in d.records(p["id"]) if r["id"] == failed)
    assert pd.usable(row, s.settings())
    assert row["raw_price"] is None and row["price"] == 9500
    assert pd.identity(row) == "123:101:regular"
    assert client.post(f"/api/records/{failed}/undo", json={}).status_code == 200
    row = next(r for r in d.records(p["id"]) if r["id"] == failed)
    assert not pd.usable(row, s.settings())
