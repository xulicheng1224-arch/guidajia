"""Point-in-time daily features and matured labels; no interpolation or APIs."""

import math
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from .product_urls import identity as product_identity

TZ = ZoneInfo("Asia/Shanghai")
VERSION = "daily-sku-v5-equal-human-priority"
MIN_HISTORY_DAYS = 7
MIN_COVERAGE = 0.7
MAX_GAP_DAYS = 3
CATEGORICAL = ["category", "brand", "shop_type", "basis"]
NUMERIC = [
    "log_price",
    "age_days",
    "return_1d",
    "return_7d",
    "return_30d",
    "distance_min_7d",
    "distance_min_30d",
    "distance_median_7d",
    "distance_median_30d",
    "percentile_30d",
    "volatility_7d",
    "changes_7d",
    "days_since_change",
    "coverage_7d",
    "coverage_30d",
    "peer_gap",
    "peer_count",
    "event_days",
    "weekday_sin",
    "weekday_cos",
    "month_sin",
    "month_cos",
    "capacity_gb",
    "release_age_days",
]
FEATURES = NUMERIC + CATEGORICAL
BASIS_NAMES = {
    "regular": "普通价",
    "subsidy": "补贴价",
    "coupon": "券后价",
    "member": "会员价",
    "reference": "表格参考价",
}


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def day(value):
    return timestamp(value).astimezone(TZ).date()


def end_of_day(value):
    return datetime.combine(value + timedelta(days=1), time.min, TZ).astimezone(
        timezone.utc
    )


def price_basis(row):
    if row.get("price_basis") in BASIS_NAMES:
        return row["price_basis"]
    price_type = row.get("price_type", "regular")
    if price_type in ("coupon", "member"):
        return price_type
    texts = " ".join(m.get("display_text", "") for m in row.get("methods", {}).values())
    return (
        "subsidy"
        if any(word in texts for word in ("补贴", "加补", "国补"))
        else "regular"
    )


def platform_id(value):
    """Canonical decimal IDs; never infer identifiers from names/spec labels."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text) or not int(text):
        return None
    return str(int(text))


def human_price(row):
    return bool(
        row.get("manual")
        or row.get("corrected")
        or row.get("effective_manual")
        or row.get("source") in ("manual", "import")
    )


def effective_sku(row):
    requested = platform_id(row.get("requested_sku_id"))
    selected = platform_id(row.get("selected_sku_id"))
    # A human correction is the confirmed price for the tracked SKU. Preserve
    # any conflicting original crawler evidence for diagnostics only.
    if human_price(row) and requested:
        return requested
    # Corrupt or contradictory evidence must not create a training series.
    for field in ("requested_sku_id", "selected_sku_id"):
        if row.get(field) not in (None, "") and platform_id(row[field]) is None:
            return None
    if requested and selected and requested != selected:
        return None
    if requested:
        return requested
    # A link without skuId can switch default variants between visits. Only an
    # observed, verified SKU may identify that visit; there is no "default" SKU.
    return selected if row.get("sku_verified") is True else None


def identity(row):
    if (
        row.get("reference_product_id")
        and row["reference_product_id"] == row.get("product_id")
        and human_price(row)
        and price_basis(row) == "reference"
        and not any(
            row.get(k) for k in ("item_id", "requested_sku_id", "selected_sku_id")
        )
    ):
        return reference_key(row["product_id"])
    item, sku = platform_id(row.get("item_id")), effective_sku(row)
    prefix = "jd:" if row.get("platform") == "jd" else ""
    return f"{prefix}{item}:{sku}:{price_basis(row)}" if item and sku else None


def reference_key(product_id):
    return f"reference:{product_id}:reference"


def matches_product(row, product):
    if row.get("reference_product_id"):
        return row["reference_product_id"] == product.get("id")
    info = product_identity(product.get("url", ""))
    item, sku = info["item_id"], info["sku_id"]
    if row.get("platform", "taobao") != info["platform"]:
        return False
    if row.get("item_id") is not None and platform_id(row["item_id"]) != item:
        return False
    if not sku:
        return True
    # Keep unsuccessful attempts for the requested SKU in the rule timeline.
    # A previously untargeted but verified visit also belongs to this SKU.
    return platform_id(row.get("requested_sku_id")) == sku or (
        row.get("sku_verified") is True and effective_sku(row) == sku
    )


def usable(row, config):
    price = row.get("price")
    human = human_price(row)
    return (
        row.get("source") in ("crawler", "api", "manual", "import")
        and row.get("valid", row.get("status") == "success")
        and (row.get("status") == "success" or human)
        and not row.get("exclude_prediction")
        and row.get("stock") not in ("out_of_stock", "delisted")
        and isinstance(price, (int, float))
        and not isinstance(price, bool)
        and math.isfinite(price)
        and price > 0
        and (
            human or not row.get("requested_sku_id") or row.get("sku_verified") is True
        )
        and (
            config.get("include_coupon")
            or row.get("price_type", "regular") == "regular"
        )
        and identity(row) is not None
    )


def daily_series(rows, config, as_of):
    """One closing price and observed minimum per day / SKU / price basis."""
    from .price_resolution import resolve

    groups = defaultdict(dict)
    for row in sorted(
        resolve(rows, as_of),
        key=lambda r: (timestamp(r["observed_at"]), r.get("id", "")),
    ):
        if timestamp(row["observed_at"]) > as_of or not usable(row, config):
            continue
        key = (row["product_id"], identity(row))
        when = day(row["observed_at"])
        previous = groups[key].get(when)
        groups[key][when] = {
            "day": when,
            "price": row["price"],
            "minimum": (
                min(previous["minimum"], row["price"]) if previous else row["price"]
            ),
            "observed_at": row["observed_at"],
            "row_id": row.get("id"),
            "context": {
                **dict(row.get("product_context") or {}),
                "observed_spec": " / ".join(row.get("selected_options", [])),
            },
            "basis": price_basis(row),
        }
    return {
        key: sorted(values.values(), key=lambda b: b["day"])
        for key, values in groups.items()
    }


def history_at(bars, cutoff):
    # Closing prices from the cutoff day may include later observations; only
    # completed days are used for training. Inference rebuilds bars at its cutoff.
    return [b for b in bars if timestamp(b["observed_at"]) <= cutoff]


def feature_row(key, series, events, cutoff):
    history = history_at(series[key], cutoff)
    if not history:
        return None
    latest = history[-1]
    today, current = day(cutoff - timedelta(microseconds=1)), latest["price"]
    age = (today - history[0]["day"]).days + 1
    if age < MIN_HISTORY_DAYS or (today - latest["day"]).days > 2:
        return None
    context = latest["context"]
    values = {name: context.get(name, "") or "unknown" for name in CATEGORICAL}
    values.update(basis=latest["basis"], log_price=math.log(current), age_days=age)
    capacity = re.search(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*(TB|GB|G)\b",
        context.get("spec") or context.get("observed_spec", ""),
        re.I,
    )
    values["capacity_gb"] = (
        float(capacity[1]) * (1024 if capacity[2].upper() == "TB" else 1)
        if capacity
        else None
    )
    try:
        released = date.fromisoformat(context.get("release_date", ""))
        values["release_age_days"] = max(0, (today - released).days)
    except (ValueError, TypeError):
        values["release_age_days"] = None
    windows = {}
    for length in (7, 30):
        window = [
            b
            for b in history
            if today - timedelta(days=length - 1) <= b["day"] <= today
        ]
        windows[length] = window
        prices = [b["price"] for b in window]
        values[f"coverage_{length}d"] = len(window) / length
        values[f"distance_min_{length}d"] = (
            current / min(prices) - 1 if prices else None
        )
        values[f"distance_median_{length}d"] = (
            current / statistics.median(prices) - 1 if prices else None
        )
    if len(windows[7]) < 4:
        return None
    for lag in (1, 7, 30):
        target = today - timedelta(days=lag)
        before = [
            b for b in history if target - timedelta(days=2) <= b["day"] <= target
        ]
        values[f"return_{lag}d"] = current / before[-1]["price"] - 1 if before else None
    prices = [b["price"] for b in windows[30]]
    values["percentile_30d"] = (
        sum(p < current for p in prices) / len(prices) if prices else None
    )
    returns = [
        math.log(b["price"] / a["price"])
        for a, b in zip(windows[7], windows[7][1:])
        if (b["day"] - a["day"]).days <= 2
    ]
    values["volatility_7d"] = statistics.pstdev(returns) if returns else 0
    values["changes_7d"] = sum(abs(r) > 1e-8 for r in returns)
    changes = [
        b["day"] for a, b in zip(history, history[1:]) if a["price"] != b["price"]
    ]
    values["days_since_change"] = (today - changes[-1]).days if changes else age
    peers = []
    if context.get("model") and context.get("spec"):
        for peer_key, bars in series.items():
            if peer_key[0] == key[0]:
                continue
            previous = history_at(bars, cutoff)
            if not previous:
                continue
            peer = previous[-1]
            other = peer["context"]
            if (
                (today - peer["day"]).days <= 2
                and peer["basis"] == latest["basis"]
                and all(other.get(k) == context.get(k) for k in ("model", "spec"))
            ):
                peers.append(peer["price"])
    values["peer_gap"] = current / statistics.median(peers) - 1 if peers else None
    values["peer_count"] = len(peers)
    known = []
    for event in events:
        known_at = (
            event.get("updated_at") or event.get("known_at") or event.get("created_at")
        )
        if (
            not known_at
            or timestamp(known_at) > cutoff
            or event.get("confidence") != "官宣"
            or event.get("ignored")
        ):
            continue
        if event.get("known_at") and timestamp(event["known_at"]) > cutoff:
            continue
        scope = event.get("scope", "all")
        applies = (
            scope == "all"
            or (scope == "product" and event.get("scope_value") == key[0])
            or (
                scope in ("category", "brand")
                and context.get(scope) == event.get("scope_value")
            )
        )
        until = (date.fromisoformat(event["start"]) - today).days
        if applies and 0 <= until <= 60:
            known.append(until)
    values["event_days"] = min(known) if known else None
    values.update(
        weekday_sin=math.sin(today.weekday() * 2 * math.pi / 7),
        weekday_cos=math.cos(today.weekday() * 2 * math.pi / 7),
        month_sin=math.sin(today.month * 2 * math.pi / 12),
        month_cos=math.cos(today.month * 2 * math.pi / 12),
    )
    return values


def future_outcome(bars, origin, horizon, as_of):
    """Missing windows are unknown, never negative training labels."""
    finish = origin + timedelta(days=horizon)
    if end_of_day(finish) > as_of:
        return None
    future = [b for b in bars if origin < b["day"] <= finish]
    if len(future) < math.ceil(horizon * MIN_COVERAGE):
        return None
    dates = {b["day"] for b in future}
    gap = 0
    for offset in range(1, horizon + 1):
        gap = 0 if origin + timedelta(days=offset) in dates else gap + 1
        if gap > MAX_GAP_DAYS:
            return None
    return {
        "minimum": min(b["minimum"] for b in future),
        "coverage": len(future) / horizon,
        "label_end": end_of_day(finish),
    }


def training_samples(series, events, config, as_of):
    samples = []
    horizon = config["forecast_days"]
    threshold = config["drop_threshold"] / 100
    for key, bars in series.items():
        for bar in bars:
            if bar["day"] < day(as_of) - timedelta(days=365):
                continue
            cutoff = end_of_day(bar["day"])
            outcome = future_outcome(bars, bar["day"], horizon, as_of)
            if not outcome:
                continue
            features = feature_row(key, series, events, cutoff)
            if not features:
                continue
            ratio = outcome["minimum"] / bar["price"]
            samples.append(
                {
                    "series": key,
                    "origin": cutoff,
                    "label_end": outcome["label_end"],
                    "price": bar["price"],
                    "features": features,
                    "target": int(ratio <= 1 - threshold),
                    "log_ratio": math.log(ratio),
                    "coverage": outcome["coverage"],
                }
            )
    return sorted(samples, key=lambda r: (r["origin"], r["series"]))


def summary(series, samples):
    bars = [b for values in series.values() for b in values]
    positives = sum(r["target"] for r in samples)
    return {
        "series": len(series),
        "daily_points": len(bars),
        "observed_days": len({b["day"] for b in bars}),
        "samples": len(samples),
        "origin_days": len({r["origin"] for r in samples}),
        "positive": positives,
        "negative": len(samples) - positives,
    }
