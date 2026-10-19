import json
import math
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend import domain as d, prediction as engine, prediction_data as pd, store as s
from tests.test_system import client, product, record

START = datetime(2024, 1, 1, 12, tzinfo=pd.TZ)


def observation(index, price=10000, product_id="p1", sku="101", **values):
    return {
        "id": f"{product_id}-{sku}-{index}-{values.get('price_basis','regular')}",
        "product_id": product_id,
        "observed_at": (START + timedelta(days=index)).isoformat(),
        "price": price,
        "status": "success",
        "valid": True,
        "stock": "in_stock",
        "source": "crawler",
        "item_id": "123",
        "requested_sku_id": sku,
        "sku_verified": True,
        "price_type": "regular",
        "product_context": {
            "category": "显卡",
            "brand": "测试品牌",
            "model": "GPU",
            "spec": "8GB",
            "shop_type": "普通店",
        },
        **values,
    }


def configuration(**values):
    return {**s.DEFAULTS, **values}


def test_daily_sampling_is_not_inflated_by_manual_recrawls():
    rows = [observation(i, 10000 - i * 5) for i in range(40)]
    cutoff = pd.end_of_day(pd.day(rows[25]["observed_at"]))
    baseline = pd.daily_series(rows, configuration(), START + timedelta(days=45))
    repeats = [
        {
            **rows[12],
            "id": f"repeat-{i}",
            "observed_at": (START + timedelta(days=12, minutes=i)).isoformat(),
        }
        for i in range(100)
    ]
    repeated = pd.daily_series(
        rows + repeats, configuration(), START + timedelta(days=45)
    )
    key = next(iter(baseline))
    assert len(repeated[key]) == len(baseline[key])
    assert pd.feature_row(key, baseline, [], cutoff) == pd.feature_row(
        key, repeated, [], cutoff
    )


def test_sku_and_price_basis_never_share_future_labels():
    rows = [observation(i, 10000) for i in range(40)]
    rows += [observation(i, 1000, sku="202") for i in range(40)]
    rows += [observation(i, 2000, price_basis="subsidy") for i in range(40)]
    series = pd.daily_series(rows, configuration(), START + timedelta(days=45))
    assert len(series) == 3
    normal = series[("p1", "123:101:regular")]
    outcome = pd.future_outcome(
        normal, pd.day(rows[10]["observed_at"]), 14, START + timedelta(days=45)
    )
    assert outcome["minimum"] == 10000


def test_observed_sku_replaces_unsafe_default_group_and_reuses_targeted_history():
    rows = [
        observation(0, sku=None, selected_sku_id="101"),
        observation(1, sku=None, selected_sku_id="202", price=20000),
        observation(2, sku="101", selected_sku_id="101"),
    ]
    series = pd.daily_series(rows, configuration(), START + timedelta(days=4))
    assert set(series) == {("p1", "123:101:regular"), ("p1", "123:202:regular")}
    assert len(series[("p1", "123:101:regular")]) == 2
    targeted = {"url": "https://item.taobao.com/item.htm?id=123&skuId=101"}
    assert pd.matches_product(rows[0], targeted)
    assert not pd.matches_product(rows[1], targeted)
    untargeted = {"url": "https://item.taobao.com/item.htm?id=123"}
    assert pd.matches_product(rows[2], untargeted)
    assert not pd.matches_product(rows[0], {"url": "https://item.taobao.com/?id=999"})


def test_unidentified_or_conflicting_skus_never_enter_training():
    for values in (
        {"requested_sku_id": None},
        {"requested_sku_id": None, "selected_sku_id": "101", "sku_verified": False},
        {"selected_sku_id": "202"},
        {"selected_sku_id": "invalid"},
        {"requested_sku_id": True},
        {"item_id": 123.0},
    ):
        row = observation(0, **values)
        assert pd.identity(row) is None
        assert not pd.usable(row, configuration())
    # ID input formatting does not split a price history into different series.
    row = observation(0, item_id=123, requested_sku_id=" 00101 ", selected_sku_id=101)
    assert pd.identity(row) == "123:101:regular"
    assert pd.matches_product(row, {"url": "https://item.taobao.com/?id=123&skuId=101"})


def test_names_and_missing_model_spec_do_not_hide_training_history():
    rows = [
        observation(i, name="随便命名" if i % 2 else "5060", product_context={})
        for i in range(40)
    ]
    at = START + timedelta(days=45)
    series = pd.daily_series(rows, configuration(), at)
    assert list(series) == [("p1", "123:101:regular")]
    assert len(next(iter(series.values()))) == 40
    samples = pd.training_samples(series, [], configuration(), at)
    assert samples
    cutoff = pd.end_of_day(pd.day(rows[20]["observed_at"]))
    features = pd.feature_row(next(iter(series)), series, [], cutoff)
    assert features["peer_count"] == 0
    assert features["category"] == "unknown"


def test_missing_or_unmatured_windows_are_unknown():
    rows = [observation(i) for i in range(30)]
    origin = pd.day(rows[7]["observed_at"])
    series = pd.daily_series(rows, configuration(), START + timedelta(days=35))
    bars = next(iter(series.values()))
    assert pd.future_outcome(bars, origin, 14, START + timedelta(days=15)) is None
    sparse = [
        b
        for b in bars
        if b["day"] not in {origin + timedelta(days=i) for i in range(1, 6)}
    ]
    assert pd.future_outcome(sparse, origin, 14, START + timedelta(days=35)) is None
    assert (
        pd.future_outcome(bars, origin, 14, START + timedelta(days=35))["coverage"] == 1
    )


def test_features_do_not_see_future_prices_peers_or_announcements():
    rows = [observation(i, 10000 + i) for i in range(45)]
    peers = [observation(i, 9900, product_id="p2", sku="202") for i in range(45)]
    cutoff = pd.end_of_day(pd.day(rows[20]["observed_at"]))
    events = [
        {
            "start": (pd.day(cutoff) + timedelta(days=4)).isoformat(),
            "confidence": "官宣",
            "scope": "all",
            "known_at": (cutoff + timedelta(days=1)).isoformat(),
        }
    ]
    before = pd.daily_series(rows + peers, configuration(), START + timedelta(days=50))
    changed = [
        {**r, "price": 100 if pd.timestamp(r["observed_at"]) > cutoff else r["price"]}
        for r in rows + peers
    ]
    after = pd.daily_series(changed, configuration(), START + timedelta(days=50))
    key = ("p1", "123:101:regular")
    assert pd.feature_row(key, before, events, cutoff) == pd.feature_row(
        key, after, events, cutoff
    )
    assert pd.feature_row(key, before, events, cutoff)["event_days"] is None


def test_invalid_and_unverified_crawls_are_excluded_but_human_prices_are_valid():
    for values in (
        {"price": None},
        {"status": "timeout"},
        {"sku_verified": False},
        {"exclude_prediction": True},
        {"stock": "out_of_stock"},
        {"price_type": "coupon"},
    ):
        assert not pd.usable(observation(0, **values), configuration())
    assert pd.usable(
        observation(0, price_type="coupon"), configuration(include_coupon=True)
    )
    for values in (
        {"corrected": True, "sku_verified": False},
        {"manual": True, "source": "manual", "sku_verified": False},
        {"source": "import", "sku_verified": False},
        {"corrected": True, "status": "sku_mismatch", "selected_sku_id": "202"},
    ):
        row = observation(0, **values)
        assert pd.usable(row, configuration())
        assert pd.identity(row) == "123:101:regular"
        assert not pd.usable({**row, "valid": False}, configuration())
        assert not pd.usable({**row, "exclude_prediction": True}, configuration())


def synthetic_history():
    rows = []
    for p, phase in (("p1", 0), ("p2", 30)):
        for index in range(410):
            price = round(10000 + 1800 * math.sin((index + phase) * 2 * math.pi / 60))
            rows.append(
                observation(
                    index, price, product_id=p, sku="101" if p == "p1" else "202"
                )
            )
    return rows


def test_purged_time_splits_have_no_overlapping_label_windows():
    from backend.prediction_training import rolling_folds

    at = START + timedelta(days=411)
    series = pd.daily_series(synthetic_history(), configuration(), at)
    samples = pd.training_samples(series, [], configuration(), at)
    folds = rolling_folds(samples, 14)
    assert len(folds) >= 2
    seen = set()
    for train, calibration, test in folds:
        assert max(r["label_end"] for r in train) <= min(
            r["origin"] for r in calibration
        )
        assert max(r["label_end"] for r in calibration) <= min(
            r["origin"] for r in test
        )
        dates = {r["origin"] for r in test}
        assert not dates & seen
        seen |= dates


def test_cold_start_report_and_training_never_invent_probability(client):
    p = product(client)
    record(p)
    response = client.get("/api/forecast-model")
    assert response.status_code == 200
    assert response.json()["active"] is None
    assert response.json()["progress"]["samples"] == 0
    result = engine.train()
    assert result["state"] == "insufficient"
    assert not (s.DATA / "forecast-models" / "active.json").exists()
    f = client.get(f'/api/product/{p["id"]}').json()["product"]["forecast"]
    assert f["probability"] is None and f["interval"] is None


def test_native_training_persistence_shadow_mode_and_config_invalidation(
    client, monkeypatch
):
    at = START + timedelta(days=411)
    rows = [
        (
            {**r, "source": "manual", "manual": True, "sku_verified": False}
            if i % 2
            else {**r, "corrected": True}
        )
        for i, r in enumerate(synthetic_history())
    ]
    series = pd.daily_series(rows, configuration(), at)
    pack = {"series": series, "events": [], "rows": rows}
    monkeypatch.setattr(engine, "now", lambda: at)
    monkeypatch.setattr(engine, "read_pack", lambda *args, **kwargs: pack)
    result = engine.train()
    assert result["state"] == "active", result
    metadata = engine.active_report()
    assert metadata and (metadata["probability_ready"] or metadata["interval_ready"])
    assert metadata["metrics"]["samples"] >= 60
    fallback = {
        "label": "数据不足",
        "factors": [],
        "window": 14,
        "observed_days": 410,
        "changes": 20,
    }
    own = [r for r in rows if r["product_id"] == "p1"]
    forecast = engine.predict({"id": "p1"}, own, fallback)
    assert forecast["model"] == "lightgbm-v1", forecast
    assert forecast["label"] == fallback["label"]
    assert forecast["model_status"] == "shadow"
    if metadata["probability_ready"]:
        assert 0 <= forecast["probability"] <= 100
    if metadata["interval_ready"]:
        assert (
            0
            < forecast["interval"][0]
            <= forecast["expected_min"]
            <= forecast["interval"][1]
        )
    artifact = s.DATA / "forecast-models" / metadata["id"] / "classifier.txt"
    intact = artifact.read_bytes()
    artifact.write_text("damaged")
    assert engine.active_report() is None
    assert engine.predict({"id": "p1"}, own, fallback)["probability"] is None
    artifact.write_bytes(intact)
    changed = configuration(drop_threshold=5)
    assert engine.active_report(changed) is None
    historical = engine.predict(
        {"id": "p1"}, own, fallback, as_of=at - timedelta(days=1)
    )
    assert historical["probability"] is None and historical["interval"] is None
    # A human correction invalidates the trained artifact until retraining.
    with s.connection() as db:
        db.execute(
            "INSERT INTO audit(id,entity_id,kind,action,reason,created_at) VALUES('correction','r','correction','update','test correction',?)",
            (s.now(),),
        )
    assert engine.active_report() is None


def test_forecast_uses_current_link_and_freezes_record_context(client):
    p = product(
        client,
        url="https://item.taobao.com/item.htm?id=123&skuId=101",
        model="5060",
        spec="8GB",
    )
    record(p, sku_verified=True)
    row = d.records(p["id"])[0]
    assert row["product_context"]["model"] == "5060"
    assert row["requested_sku_id"] == "101"
    assert pd.matches_product(row, p)
    different = {**p, "url": "https://item.taobao.com/item.htm?id=123&skuId=202"}
    f = d.forecast(different, [row])
    assert f["current_price"] is None


def test_price_basis_detects_legacy_subsidy_text():
    assert (
        pd.price_basis({"methods": {"re": {"display_text": "平台加补后 ￥3929"}}})
        == "subsidy"
    )
    assert pd.price_basis({"price_type": "coupon"}) == "coupon"


def test_daily_close_orders_absolute_time_across_offsets():
    rows = [
        observation(0, 10000, observed_at="2024-01-01T10:00:00+08:00"),
        observation(0, 9000, observed_at="2024-01-01T03:00:00+00:00"),
    ]
    bars = pd.daily_series(rows, configuration(), START + timedelta(days=2))
    assert next(iter(bars.values()))[0]["price"] == 9000


def test_invalid_model_metadata_falls_back_without_server_error(client):
    folder = engine.root()
    (folder / "active.json").write_text('["damaged metadata"]')
    assert engine.active_report() is None
    response = client.get("/api/forecast-model")
    assert response.status_code == 200
    assert response.json()["active"] is None


def test_background_training_is_deduplicated_and_respects_auto_switch(
    client, monkeypatch
):
    calls = []

    def training():
        calls.append(1)
        return {"state": "insufficient"}

    monkeypatch.setattr(engine, "train", training)
    s.set_settings({"ml_auto_train": False})

    async def run():
        assert not await engine.schedule()
        assert await engine.schedule(force=True)
        assert not await engine.schedule(force=True)
        await engine.TRAINING
        assert calls == [1]

    asyncio.run(run())


def test_live_review_preserves_target_threshold_and_requires_coverage(
    client, monkeypatch
):
    p = product(client, url="https://item.taobao.com/item.htm?id=123&skuId=101")
    as_of = START + timedelta(days=40)
    monkeypatch.setattr(d, "utcnow", lambda: as_of)
    rows = [
        observation(i, 10000 if i == 0 else 9000, product_id=p["id"]) for i in range(20)
    ]
    with s.connection() as db:
        for row in rows:
            extra = {
                k: v
                for k, v in row.items()
                if k
                not in (
                    "id",
                    "product_id",
                    "observed_at",
                    "price",
                    "status",
                    "stock",
                    "valid",
                )
            }
            db.execute(
                "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    p["id"],
                    row["observed_at"],
                    row["price"],
                    None,
                    "in_stock",
                    "success",
                    s.dumps(extra),
                    None,
                ),
            )
        snapshot = {
            "label": "建议观望",
            "current_price": 10000,
            "window": 14,
            "drop_threshold": 3,
            "series_key": "123:101:regular",
            "model": "lightgbm-v1",
            "probability": 80,
            "interval": [8500, 9500],
        }
        db.execute(
            "INSERT INTO forecasts VALUES(?,?,?,?)",
            ("test-forecast", p["id"], START.isoformat(), s.dumps(snapshot)),
        )
    s.set_settings({"drop_threshold": 20})
    review = client.get("/api/forecast-review").json()[0]
    assert review["drop_rate"] == 100 and review["interval_coverage"] == 100
    assert review["brier"] == 0.04
    with s.connection() as db:
        db.execute(
            "DELETE FROM observations WHERE observed_at>?",
            ((START + timedelta(days=2)).isoformat(),),
        )
    assert client.get("/api/forecast-review").json() == []
