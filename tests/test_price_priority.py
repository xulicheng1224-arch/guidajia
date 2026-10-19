"""Human data has equal training status and higher effective price precedence."""

import json
from datetime import datetime, timedelta

from backend import domain as d, prediction_data as pd, store as s
from tests.test_system import client, product


def tracked(client):
    return product(client, url="https://item.taobao.com/item.htm?id=123&skuId=101")


def day():
    return (pd.day(d.utcnow()) - timedelta(days=1)).isoformat()


def record(p, price, hour, source="crawler", **extra):
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
        observed_at=f"{day()}T{hour:02}:00:00+08:00",
    )


def test_manual_entry_overrides_later_crawl_in_chart_stats_daily_and_training(client):
    p = tracked(client)
    early = record(p, 10000, 8)
    manual = record(p, 12000, 12, source="manual")
    late = record(p, 9000, 20)
    detail = client.get(f"/api/product/{p['id']}").json()
    rows = {r["id"]: r for r in detail["records"]}
    assert {r["price"] for r in rows.values()} == {12000}
    assert {r["chart_price"] for r in rows.values()} == {12000}
    assert rows[early]["raw_price"] == 10000 and rows[late]["raw_price"] == 9000
    assert rows[late]["human_override_observation_id"] == manual
    assert detail["product"]["current_price"] == detail["product"]["lowest"] == 12000
    assert detail["product"]["current_price_manual"]
    daily = client.get(f"/api/product/{p['id']}/daily-prices?basis=regular").json()
    assert daily["rows"][0]["price"] == 12000
    bars = pd.daily_series(detail["records"], s.settings(), d.utcnow())
    bar = next(iter(bars.values()))[0]
    assert bar["price"] == bar["minimum"] == 12000
    assert d.forecast(s.get(p["id"]), detail["records"])["current_price"] == 12000


def test_price_edit_wins_import_and_day_edit_wins_point_edit_with_reversible_undo(
    client,
):
    p = tracked(client)
    crawl = record(p, 10000, 8)
    record(p, 11000, 20, source="import")
    client.post(
        f"/api/records/{crawl}/correct", json={"price": 130, "reason": "人工复核"}
    ).raise_for_status()
    assert {r["price"] for r in d.records(p["id"])} == {13000}
    edited = (
        client.post(
            f"/api/product/{p['id']}/daily-prices",
            json={
                "day": day(),
                "basis": "regular",
                "price": 140,
                "reason": "确认当日价格",
                "item_id": "123",
                "sku_id": "101",
            },
        )
        .raise_for_status()
        .json()
    )
    assert {r["price"] for r in d.records(p["id"])} == {14000}
    client.post(
        f"/api/daily-prices/{edited['id']}/undo", json={"version": edited["version"]}
    ).raise_for_status()
    assert {r["price"] for r in d.records(p["id"])} == {13000}
    client.post(f"/api/records/{crawl}/undo").raise_for_status()
    assert {r["price"] for r in d.records(p["id"])} == {11000}
    assert next(r for r in d.records(p["id"]) if r["id"] == crawl)["raw_price"] == 10000


def test_equal_priority_uses_save_order_for_backdated_manual_prices(
    client, monkeypatch
):
    p = tracked(client)
    monkeypatch.setattr(s, "now", lambda: day() + "T21:00:00+08:00")
    record(p, 11000, 20, source="manual")
    monkeypatch.setattr(s, "now", lambda: day() + "T22:00:00+08:00")
    recent = record(p, 12000, 9, source="manual")
    rows = d.records(p["id"])
    assert {r["price"] for r in rows} == {12000}
    assert rows[-1]["human_override_observation_id"] == recent


def test_priority_never_crosses_day_sku_basis_or_product(client):
    p = tracked(client)
    another = product(client, url="https://item.taobao.com/item.htm?id=999&skuId=101")
    manual = record(p, 12000, 8, source="manual")
    other_sku = record(p, 11000, 10, requested_sku_id="202", selected_sku_id="202")
    coupon = record(p, 10000, 10, price_basis="subsidy")
    other_product = record(another, 10000, 10)
    earlier = d.save_observation(
        p["id"],
        {
            "price": 10000,
            "status": "success",
            "stock": "in_stock",
            "sku_verified": True,
        },
        observed_at=(pd.day(d.utcnow()) - timedelta(days=2)).isoformat()
        + "T10:00:00+08:00",
    )
    rows = {r["id"]: r for r in d.records()}
    assert rows[manual]["price"] == 12000
    assert [rows[i]["price"] for i in (other_sku, coupon, other_product, earlier)] == [
        11000,
        10000,
        10000,
        10000,
    ]


def test_invalid_or_excluded_values_are_not_reintroduced_by_priority(client):
    p = tracked(client)
    crawl = record(p, 10000, 8, exclude_prediction=True)
    manual = record(p, 12000, 12, source="manual")
    invalid = record(p, 13000, 16, source="manual")
    client.post(
        f"/api/records/{invalid}/correct", json={"valid": False, "reason": "误录"}
    ).raise_for_status()
    rows = {r["id"]: r for r in d.records(p["id"])}
    assert not pd.usable(rows[crawl], s.settings())
    assert rows[crawl]["price"] == 12000
    assert rows[invalid]["valid"] is False and rows[invalid]["price"] == 13000
    assert pd.usable(rows[manual], s.settings())
    assert rows[manual]["price"] == 12000


def test_historical_as_of_does_not_use_later_manual_observation(client):
    p = tracked(client)
    crawl = record(p, 10000, 8)
    record(p, 12000, 20, source="manual")
    rows = d.records(p["id"])
    assert next(r for r in rows if r["id"] == crawl)["price"] == 12000
    cutoff = pd.timestamp(day() + "T12:00:00+08:00")
    early = next(iter(pd.daily_series(rows, s.settings(), cutoff).values()))[0]
    assert early["price"] == early["minimum"] == 10000
    assert d.forecast(s.get(p["id"]), rows, as_of=cutoff)["current_price"] == 10000
    now = next(iter(pd.daily_series(rows, s.settings(), d.utcnow()).values()))[0]
    assert now["price"] == now["minimum"] == 12000


def test_imported_forward_values_have_same_features_and_labels_as_crawls(client):
    # Provenance cannot change sample count, labels or weights for the same data.
    start = pd.timestamp("2026-01-01T12:00:00+08:00")
    rows = [
        {
            "id": str(i),
            "product_id": "p1",
            "observed_at": (start + timedelta(days=i)).isoformat(),
            "price": 10000 - i * 100,
            "status": "success",
            "valid": True,
            "stock": "in_stock",
            "source": "crawler",
            "item_id": "123",
            "requested_sku_id": "101",
            "sku_verified": True,
            "price_type": "regular",
        }
        for i in range(40)
    ]
    imported = [
        {**r, "source": "import", "data_quality": "carried_forward"} for r in rows
    ]
    at = start + timedelta(days=45)
    a = pd.daily_series(rows, s.settings(), at)
    b = pd.daily_series(imported, s.settings(), at)
    assert a == b
    samples_a = pd.training_samples(a, [], s.settings(), at)
    samples_b = pd.training_samples(b, [], s.settings(), at)
    assert samples_a and samples_a == samples_b


def test_import_equal_to_existing_crawl_still_establishes_human_priority(client):
    p = tracked(client)
    record(p, 10000, 8)
    payload = json.dumps(
        [
            {
                "product_id": p["id"],
                "observed_at": day() + "T08:00:00+08:00",
                "price": 10000,
                "price_unit": "cent",
            }
        ]
    )
    first = client.post(
        "/api/import/observations", json={"text": payload, "preview": False}
    ).json()
    assert first["new"] == 1, first
    record(p, 11000, 20)
    assert {r["price"] for r in d.records(p["id"])} == {10000}
    again = client.post(
        "/api/import/observations", json={"text": payload, "preview": False}
    ).json()
    assert again["new"] == 0 and again["skipped"] == 1
