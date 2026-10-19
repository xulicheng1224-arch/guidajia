import json
import statistics
import time
from functools import lru_cache
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from diagnostics import describe
from collection_limits import COLLECT_TIMEOUT_SECONDS

from . import prediction_data as pd, store as s


def dt(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def utcnow():
    return datetime.now(timezone.utc)


def records(product_id=None):
    config = s.settings()
    with s.connection() as db:
        rows = db.execute(
            "SELECT * FROM observations"
            + (" WHERE product_id=?" if product_id else "")
            + " ORDER BY observed_at,id",
            (product_id,) if product_id else (),
        ).fetchall()
        manual_times = {
            r["entity_id"]: r["stamp"]
            for r in db.execute(
                "SELECT entity_id, max(created_at) stamp FROM audit WHERE kind='manual' AND action='create' GROUP BY entity_id"
            )
        }
    corrections = {r["observation_id"]: r for r in s.entities("correction")}
    result = []
    for row in rows:
        raw = dict(row)
        extra = json.loads(raw.pop("data"))
        override = corrections.get(raw["id"])
        raw_price = raw["price"]
        item = {
            **extra,
            **raw,
            "raw_price": raw_price,
            "raw_original_price": raw["original_price"],
            "valid": raw["status"] == "success",
            "manual": extra.get("source") in ("manual", "import"),
            "recorded_at": extra.get("recorded_at")
            or manual_times.get(raw["id"])
            or raw["observed_at"],
            "corrected": bool(override),
        }
        if item.get("source") in ("crawler", "api") and not item.get("diagnostics"):
            item["diagnostics"] = describe({**item, "price": None})
        if override:
            for key in (
                "price",
                "original_price",
                "stock",
                "valid",
                "exclude_prediction",
                "reason",
            ):
                if key in override:
                    item[key] = override[key]
            item["correction_id"] = override["id"]
            if "price" in override:
                item["price_modified_at"] = override["updated_at"]
        item["chart_price"] = item["price"] if eligible(item, config) else None
        result.append(item)
    from .daily_prices import overlay_rows

    result = overlay_rows(result, product_id)
    from .price_resolution import resolve

    result = resolve(result)
    for item in result:
        item["chart_price"] = item["price"] if eligible(item, config) else None
    result.sort(key=lambda r: (dt(r["observed_at"]), r["id"]))
    return result


def eligible(row, config, coupon=None):
    return (
        row["valid"]
        and row["price"] is not None
        and row["stock"] not in ("out_of_stock", "delisted")
        and (
            (config["include_coupon"] if coupon is None else coupon)
            or row.get("price_type", "regular") == "regular"
        )
    )


def changes(rows, confirmations=2):
    """Confirmed price transitions. Failures break a confirmation streak."""
    baseline, pending, streak, start = None, None, 0, None
    result = []
    for row in rows:
        if not row["valid"] or row["price"] is None:
            pending, streak = None, 0
            continue
        price = row["price"]
        if baseline is None:
            baseline = row
            continue
        if price == baseline["price"]:
            pending, streak = None, 0
            continue
        if pending != price:
            pending, streak, start = price, 1, row
        else:
            streak += 1
        if streak >= confirmations or row.get("manual") or row.get("corrected"):
            result.append(
                {
                    "id": f'change_{start["id"]}',
                    "product_id": row["product_id"],
                    "observed_at": start["observed_at"],
                    "confirmed_at": row["observed_at"],
                    "type": "降价" if price < baseline["price"] else "涨价",
                    "old_price": baseline["price"],
                    "new_price": price,
                    "percent": round((price / baseline["price"] - 1) * 100, 2),
                    "duration_days": round(
                        (
                            dt(start["observed_at"]) - dt(baseline["observed_at"])
                        ).total_seconds()
                        / 86400,
                        2,
                    ),
                }
            )
            baseline, pending, streak = start, None, 0
    flags = {v["id"]: v for v in s.entities("change")}
    return [
        (
            {**r, **s.payload(flags[r["id"]])}
            if r["id"] in flags
            else {**r, "excluded": False, "mark": ""}
        )
        for r in result
    ]


def event_applies(event, product):
    return (
        event.get("scope") == "all"
        or (
            event.get("scope") == "product"
            and event.get("scope_value") == product["id"]
        )
        or product.get(event.get("scope")) == event.get("scope_value")
    )


def forecast(product, rows, config=None, events=None, as_of=None):
    from . import prediction

    config = config or s.settings()
    rows = [r for r in rows if pd.matches_product(r, product)]
    return prediction.predict(
        product,
        rows,
        rule_forecast(product, rows, config, events, as_of),
        config,
        as_of,
    )


def rule_forecast(product, rows, config=None, events=None, as_of=None):
    from .price_resolution import resolve

    config = config or s.settings()
    at = as_of or utcnow()
    past = resolve(rows, at)
    good = [r for r in past if eligible(r, config) and not r.get("exclude_prediction")]
    latest = good[-1] if good else None
    basis = pd.price_basis(latest) if latest else None
    series_key = pd.identity(latest) if latest else None
    timeline = [
        {
            **r,
            "valid": bool(
                eligible(r, config)
                and not r.get("exclude_prediction")
                and pd.price_basis(r) == basis
                and (not series_key or pd.identity(r) == series_key)
            ),
        }
        for r in past
    ]
    rows = [r for r in timeline if r["valid"]]
    transitions = [
        c for c in changes(timeline, config["confirm_count"]) if not c.get("excluded")
    ]
    days = (at - dt(rows[0]["observed_at"])).total_seconds() / 86400 if rows else 0
    result = {
        "label": "数据不足",
        "probability": None,
        "interval": None,
        "interval_kind": None,
        "factors": [],
        "observed_days": int(days),
        "changes": len(transitions),
        "quality": "不足",
        "model": "rules-v2",
        "window": config["forecast_days"],
        "current_price": rows[-1]["price"] if rows else None,
        "generated_at": at.isoformat(),
    }
    if days < config["min_days"] or len(transitions) < config["min_changes"]:
        result["factors"] = [
            f'已观测 {int(days)} 天，需要 {config["min_days"]} 天',
            f'已确认变价 {len(transitions)} 次，需要 {config["min_changes"]} 次',
        ]
        return result
    daily = {pd.day(r["observed_at"]): r["price"] for r in rows}
    prices = list(daily.values())
    current = rows[-1]["price"]
    percentile = 100 * sum(p < current for p in prices) / len(prices)
    known_events = [
        e
        for e in (events if events is not None else s.entities("event"))
        if e["confidence"] == "官宣"
        and not e.get("ignored")
        and dt(e.get("known_at") or e["created_at"]) <= at
        and event_applies(e, product)
        and at.date()
        <= datetime.fromisoformat(e["start"]).date()
        <= (at + timedelta(days=config["forecast_days"])).date()
    ]
    recent = [c for c in transitions if dt(c["observed_at"]) >= at - timedelta(days=7)]
    result.update(
        label="平稳",
        quality="正常",
        percentile=round(percentile, 1),
        factors=[f"当前价格处于监测价格的 {percentile:.0f}% 分位"],
    )
    if known_events:
        result.update(
            label="建议观望",
            factors=result["factors"]
            + [f'未来 {config["forecast_days"]} 天有 {known_events[0]["name"]}'],
        )
    elif percentile <= config["low_percentile"]:
        result["label"] = "接近阶段低点"
    elif len(recent) >= 2 and all(c["percent"] > 0 for c in recent[-2:]):
        result.update(
            label="可能涨价", factors=result["factors"] + ["最近两次确认变价均为上涨"]
        )
    elif recent and recent[-1]["percent"] <= -config["drop_threshold"]:
        result.update(
            label="建议观望", factors=result["factors"] + ["近期价格仍在下行"]
        )
    # Rules do not manufacture calibrated probabilities or confidence intervals.
    return result


def products():
    return _products_cached(str(s.db_path()), s.REVISION, int(time.time() // 60))


@lru_cache(maxsize=1)
def _products_cached(database, revision, minute):
    config = s.settings()
    prediction_time = utcnow()
    grouped = defaultdict(list)
    for row in records():
        grouped[row["product_id"]].append(row)
    events = s.entities("event")
    output = []
    for p in s.entities("product"):
        rows = grouped[p["id"]]
        good = [r for r in rows if eligible(r, config)]
        latest, valid = (rows[-1] if rows else None), (good[-1] if good else None)
        current_series = pd.identity(valid) if valid else None
        same_series = (
            [r for r in rows if pd.identity(r) == current_series]
            if current_series
            else rows
        )
        if current_series:
            good = [r for r in good if pd.identity(r) == current_series]
        prices = [r["price"] for r in good]
        recent90 = [
            r["price"]
            for r in good
            if dt(r["observed_at"]) >= utcnow() - timedelta(days=90)
        ]
        cutoff = utcnow() - timedelta(days=30)
        recent_changes = [
            c
            for c in changes(same_series, config["confirm_count"])
            if dt(c["observed_at"]) >= cutoff
        ]
        pred = forecast(p, rows, config, events, prediction_time)
        override = (
            p.get("override_label")
            if p.get("override_until") and dt(p["override_until"]) > utcnow()
            else None
        )
        seven = next(
            (r for r in good if dt(r["observed_at"]) >= utcnow() - timedelta(days=7)),
            None,
        )
        failures = 0
        for r in reversed(rows):
            if r["status"] == "success":
                break
            failures += 1
        output.append(
            {
                **p,
                "price": (
                    latest["price"]
                    if latest and latest["status"] == "success"
                    else None
                ),
                "current_price": valid["price"] if valid else None,
                "current_price_source": valid.get("source") if valid else None,
                "current_price_platform": valid.get("platform", "taobao") if valid else None,
                "current_price_granularity": valid.get("api_granularity") if valid else None,
                "current_price_manual": bool(
                    valid
                    and (
                        valid.get("corrected")
                        or valid.get("manual")
                        or valid.get("effective_manual")
                    )
                ),
                "current_price_daily_day": valid.get("daily_day") if valid else None,
                "original_price": valid["original_price"] if valid else None,
                "price_type": valid.get("price_type", "regular") if valid else None,
                "price_basis": pd.price_basis(valid) if valid else None,
                "current_price_quality": valid.get("data_quality") if valid else None,
                "has_reference_history": any(
                    r.get("reference_product_id") for r in rows
                ),
                "lowest": min(prices) if prices else None,
                "highest": max(prices) if prices else None,
                "lowest_90d": min(recent90) if recent90 else None,
                "highest_90d": max(recent90) if recent90 else None,
                "last_success": valid["observed_at"] if valid else None,
                "last_crawl_success": next(
                    (
                        r["observed_at"]
                        for r in reversed(rows)
                        if r.get("source") in ("crawler", "api")
                        and r["status"] == "success"
                        and r.get("raw_price") is not None
                    ),
                    None,
                ),
                "last_attempt": latest["observed_at"] if latest else None,
                "last_status": latest["status"] if latest else "not_run",
                "last_diagnostics": latest.get("diagnostics") if latest else None,
                "stock": latest["stock"] if latest else "unknown",
                "stale": bool(latest and not eligible(latest, config)),
                "changes_30d": len(recent_changes),
                "drop_7d": (
                    round((valid["price"] / seven["price"] - 1) * 100, 2)
                    if valid and seven
                    else None
                ),
                "above_low": (
                    round((valid["price"] / min(prices) - 1) * 100, 2)
                    if prices
                    else None
                ),
                "observation_count": len(rows),
                "failures": failures,
                "forecast": pred,
                "label": override or pred["label"],
                "overridden": bool(override),
            }
        )
    return output


def scope_matches(rule, product):
    scope, value = rule["scope"], rule.get("scope_value")
    return (
        scope == "all"
        or (scope == "product" and product["id"] == value)
        or (scope == "tag" and value in product.get("tags", []))
        or product.get(scope) == value
    )


def rule_matches(rule, rows, product, at=None):
    at = at or utcnow()
    config = s.settings()
    window = [r for r in rows if dt(r["observed_at"]) <= at]
    if not window or not scope_matches(rule, product):
        return False, None
    latest = window[-1]
    if rule["condition"] == "event":
        upcoming = [
            e
            for e in s.entities("event")
            if e["confidence"] == "官宣"
            and not e.get("ignored")
            and e["start"] >= at.date().isoformat()
            and e["start"] <= (at + timedelta(days=rule["days"])).date().isoformat()
            and dt(e.get("known_at") or e["created_at"]) <= at
            and event_applies(e, product)
        ]
        return bool(upcoming), latest.get("price")
    if rule["condition"] == "restock":
        if len(window) < 2:
            return False, None
        return (
            latest["stock"] == "in_stock" and window[-2]["stock"] == "out_of_stock",
            latest["price"],
        )
    if not eligible(latest, config, rule["include_coupon"]):
        return False, None
    n = rule["confirmations"]
    if len(window) < n or any(
        not eligible(r, config, rule["include_coupon"]) or r["price"] != latest["price"]
        for r in window[-n:]
    ):
        return False, None
    price = latest["price"]
    old = [
        r
        for r in window[:-n]
        if eligible(r, config, rule["include_coupon"])
        and dt(r["observed_at"]) >= at - timedelta(days=rule["days"])
    ]
    if rule["condition"] == "target":
        return price <= int(Decimal(str(rule["value"])) * 100), price
    if rule["condition"] == "low":
        return bool(old) and price < min(r["price"] for r in old), price
    if rule["condition"] == "drop":
        return bool(old) and (1 - price / old[0]["price"]) * 100 >= rule["value"], price
    return False, None


def evaluate_rules(product, rows):
    for rule in s.entities("rule"):
        if not rule["enabled"] or rule.get("template"):
            continue
        hit, price = rule_matches(rule, rows, product)
        key = f'{rule["id"]}:{product["id"]}'
        with s.connection() as db:
            state = db.execute(
                "SELECT * FROM alert_states WHERE key=?", (key,)
            ).fetchone()
            if not hit:
                db.execute(
                    "INSERT INTO alert_states(key,active) VALUES(?,0) ON CONFLICT(key) DO UPDATE SET active=0",
                    (key,),
                )
                continue
            signature = (
                str(price)
                if rule["condition"] != "event"
                else utcnow().date().isoformat()
            )
            if state and state["active"] and state["signature"] == signature:
                continue
            if (
                state
                and state["last_sent"]
                and utcnow() - dt(state["last_sent"])
                < timedelta(minutes=rule["cooldown_minutes"])
            ):
                continue
            stamp = s.now()
            message = f'{rule["name"]} · {product["name"]} · ' + (
                f"¥{price / 100:,.2f}" if price else "活动即将开始"
            )
            for channel in set(rule["channels"] + ["inapp"]):
                dedup = f'{key}:{rows[-1]["id"]}:{channel}'
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(id,rule_id,product_id,channel,message,status,created_at,dedup_key) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        s.uid(),
                        rule["id"],
                        product["id"],
                        channel,
                        message,
                        "sent" if channel == "inapp" else "pending",
                        stamp,
                        dedup,
                    ),
                )
            db.execute(
                "INSERT INTO alert_states VALUES(?,1,?,?) ON CONFLICT(key) DO UPDATE SET active=1,last_sent=excluded.last_sent,signature=excluded.signature",
                (key, stamp, signature),
            )


def save_observation(
    product_id, result, job_id=None, source="crawler", observed_at=None
):
    config = s.settings()
    price, status = result.get("price"), result.get("status", "crawler_error")
    if status != "success":
        price = None
    if job_id:
        with s.connection() as db:
            existing = db.execute(
                "SELECT id FROM observations WHERE job_id=?", (job_id,)
            ).fetchone()
        if existing:
            return existing["id"]
    history = records(product_id)
    previous = [r for r in history if eligible(r, config)]
    if status == "page_missing":
        missing = []
        for row in reversed(history):
            if row["status"] != "page_missing":
                break
            missing.append(row)
        if (
            len(missing) + 1 >= config["delist_count"]
            and missing
            and utcnow() - dt(missing[-1]["observed_at"])
            >= timedelta(hours=config["delist_hours"])
        ):
            result = {**result, "stock": "delisted"}
    stamp, record_id = observed_at or s.now(), s.uid()
    extra = {
        k: v
        for k, v in result.items()
        if k not in ("price", "original_price", "status", "stock")
    }
    extra["source"] = source
    extra["recorded_at"] = s.now()
    if source in ("crawler", "api", "manual", "import"):
        context = s.get(product_id, "product") or {}
        from .product_urls import identity
        info = identity(context.get("url", ""))
        extra.setdefault("platform", info["platform"])
        extra.setdefault("item_id", info["item_id"])
        extra.setdefault("requested_sku_id", info["sku_id"])
        extra.setdefault(
            "product_context",
            {
                k: context.get(k, "")
                for k in (
                    "category",
                    "brand",
                    "model",
                    "spec",
                    "shop_type",
                    "release_date",
                )
            },
        )
        extra.setdefault("price_basis", pd.price_basis(result))
        if not context.get("url") or result.get("price_basis") == "reference":
            if source in ("crawler", "api"):
                raise ValueError("参考商品尚未填写商品链接，不能自动采集")
            extra.update(
                reference_product_id=product_id,
                item_id=None,
                requested_sku_id=None,
                selected_sku_id=None,
                sku_verified=False,
                price_basis="reference",
            )
            extra["product_context"] = {
                **(extra.get("product_context") or {}),
                "shop_type": "",
            }
    # A newly linked store/SKU must not be compared with unrelated reference
    # prices when checking for anomalous crawler results.
    candidate = pd.identity({**extra, "product_id": product_id, "source": source})
    comparable = [r for r in previous if pd.identity(r) == candidate]
    if price is not None and source not in ("manual", "import") and comparable:
        median = statistics.median([r["price"] for r in comparable[-30:]])
        if abs(price / median - 1) * 100 > config["anomaly_percent"]:
            status = "anomaly"
    if result.get("diagnostics"):
        extra["diagnostics"] = describe({**result, "status": status})
    with s.connection() as db:
        db.execute(
            "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                product_id,
                stamp,
                price,
                result.get("original_price"),
                result.get("stock", "unknown"),
                status,
                s.dumps(extra),
                job_id,
            ),
        )
        if source in ("manual", "import"):
            s.audit(
                db,
                record_id,
                "manual",
                "create",
                None,
                {
                    "id": record_id,
                    "product_id": product_id,
                    "price": price,
                    "observed_at": stamp,
                },
                "手动新增价格",
            )
    return record_id


def health():
    from .collection_batches import active_summary
    from .collection_queue import explain

    cutoff = (utcnow() - timedelta(hours=24)).isoformat()
    with s.connection() as db:
        counts = {
            r["status"]: r["n"]
            for r in db.execute(
                "SELECT status,count(*) n FROM observations WHERE observed_at>=? AND json_extract(data,'$.source') IN ('crawler','api') GROUP BY status",
                (cutoff,),
            )
        }
        queued = db.execute(
            "SELECT count(*) FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        waiting = db.execute(
            "SELECT count(*) FROM jobs WHERE status='queued'"
        ).fetchone()[0]
        running = [
            dict(r) for r in db.execute("SELECT * FROM jobs WHERE status='running'")
        ]
        recent = db.execute(
            "SELECT max(observed_at) FROM observations WHERE status='success' AND json_extract(data,'$.source') IN ('crawler','api')"
        ).fetchone()[0]
    total = sum(counts.values())
    config = s.settings()
    return {
        "total": total,
        "success": counts.get("success", 0),
        "rate": round(counts.get("success", 0) / total * 100, 1) if total else None,
        "queued": queued,
        "running": running,
        "waiting": waiting,
        "queue": explain(),
        "max_concurrency": min(5, config["max_concurrency"]),
        "timeout_seconds": COLLECT_TIMEOUT_SECONDS,
        "batch": active_summary(),
        "last_success": recent,
        "paused": config["global_paused"],
        "circuit_until": config["circuit_until"],
        "failures": config["failures"],
        "source": "淘宝 / 京东 · 官方 API" if config.get("price_source") == "api" else "淘宝 / 天猫 · 纯爬虫",
        "price_source": config.get("price_source", "crawler"),
    }
