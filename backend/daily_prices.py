"""Reversible calendar-day price overlays, scoped to an immutable SKU and basis."""

import hashlib
from datetime import date, timedelta

from fastapi import HTTPException

from . import prediction_data as pd, store as s
from .product_urls import identity as product_identity


def scope(product, rows):
    info = product_identity(product["url"])
    item, sku = info["item_id"], info["sku_id"]
    if not sku:
        for row in reversed(rows):
            if (
                pd.platform_id(row.get("item_id")) == item
                and row.get("platform", "taobao") == info["platform"]
                and row.get("sku_verified") is True
                and pd.effective_sku(row)
            ):
                sku = pd.effective_sku(row)
                break
    return item, sku


def key(product_id, item, sku, basis, day, platform="taobao"):
    identity_item = f"jd:{item}" if platform == "jd" else item
    digest = hashlib.sha256(s.dumps([product_id, identity_item, sku, basis, day]).encode())
    return "daily_" + digest.hexdigest()


def row_key(row):
    identity = pd.identity(row)
    return (row["product_id"], identity, pd.day(row["observed_at"]).isoformat())


def overlay_rows(rows, product_id=None):
    """Keep raw columns untouched; apply after point-level corrections."""
    overrides = {
        (p["product_id"], p["series_key"], p["day"]): p
        for p in s.entities("daily_price")
        if product_id is None or p["product_id"] == product_id
    }
    used = set()
    for row in rows:
        k = row_key(row)
        override = overrides.get(k)
        if (
            not override
            or not row["valid"]
            or row["price"] is None
            or row["stock"] in ("out_of_stock", "delisted")
        ):
            continue
        # Failed/invalid/other-SKU records retain their original meaning.
        used.add(k)
        row.update(
            before_daily_price=row["price"],
            price=override["price"],
            corrected=True,
            daily_override_id=override["id"],
            daily_override_version=override["version"],
            daily_day=override["day"],
            price_modified_at=override["updated_at"],
            reason=override["reason"],
        )
    for k, override in overrides.items():
        if k in used:
            continue
        # Empty days are represented by a virtual manual observation. No fake
        # crawler record is inserted; undo removes this point completely.
        closing = pd.end_of_day(date.fromisoformat(override["day"])) - timedelta(
            microseconds=1
        )
        observed = min(closing, pd.timestamp(override["updated_at"]))
        rows.append(
            {
                "id": override["id"],
                "product_id": override["product_id"],
                "observed_at": observed.isoformat(),
                "price": override["price"],
                "raw_price": None,
                "original_price": None,
                "raw_original_price": None,
                "stock": "in_stock",
                "status": "success",
                "valid": True,
                "source": "manual",
                "manual": True,
                "corrected": True,
                "exclude_prediction": False,
                "item_id": override["item_id"],
                "platform": override.get("platform", "taobao"),
                "reference_product_id": override.get("reference_product_id"),
                "requested_sku_id": override["sku_id"],
                "sku_verified": False,
                "price_type": (
                    override["basis"]
                    if override["basis"] in ("coupon", "member")
                    else "regular"
                ),
                "price_basis": override["basis"],
                "product_context": override.get("product_context", {}),
                "daily_override_id": override["id"],
                "daily_override_version": override["version"],
                "daily_day": override["day"],
                "reason": override["reason"],
                "virtual_daily": True,
                "price_modified_at": override["updated_at"],
            }
        )
    return rows


def report(product, rows, basis):
    if not isinstance(basis, str) or basis not in pd.BASIS_NAMES:
        raise HTTPException(422, "无效价格口径")
    item, sku = (None, None) if basis == "reference" else scope(product, rows)
    series_key = (
        pd.reference_key(product["id"])
        if basis == "reference"
        else pd.identity({"platform": product_identity(product["url"])["platform"], "item_id": item, "requested_sku_id": sku, "price_basis": basis})
    )
    overrides = {
        p["day"]: p
        for p in s.entities("daily_price")
        if p["product_id"] == product["id"] and p["series_key"] == series_key
    }
    days = {}
    for row in rows:
        if not series_key or pd.identity(row) != series_key:
            continue
        day = pd.day(row["observed_at"]).isoformat()
        entry = days.setdefault(
            day, {"day": day, "price": None, "raw_price": None, "observations": 0}
        )
        if not row.get("virtual_daily"):
            entry["observations"] += 1
            if row.get("raw_price") is not None:
                entry["raw_price"] = row["raw_price"]
            entry["data_quality"] = row.get("data_quality")
            entry["source_note"] = row.get("source_note")
        if (
            row["valid"]
            and row["price"] is not None
            and row["stock"] not in ("out_of_stock", "delisted")
        ):
            entry["price"] = row["price"]
            entry["effective_manual"] = bool(row.get("effective_manual"))
    for day, override in overrides.items():
        entry = days.setdefault(day, {"day": day, "raw_price": None, "observations": 0})
        entry.update(
            price=override["price"],
            override_id=override["id"],
            version=override["version"],
            reason=override["reason"],
        )
    return {
        "item_id": item,
        "sku_id": sku,
        "basis": basis,
        "reference": basis == "reference",
        "timezone": "Asia/Shanghai",
        "rows": sorted(days.values(), key=lambda r: r["day"], reverse=True),
    }


def save(product, rows, body):
    from . import collector, domain as d

    basis = body.get("basis", "regular")
    if not isinstance(basis, str) or basis not in pd.BASIS_NAMES:
        raise HTTPException(422, "无效价格口径")
    text = body.get("day")
    try:
        day = date.fromisoformat(text)
        if day.isoformat() != text:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, "请选择有效日期，格式为 YYYY-MM-DD")
    if day > pd.day(d.utcnow()):
        raise HTTPException(422, "不能编辑未来日期的价格")
    price = collector.cents(body.get("price"))
    if price is None:
        raise HTTPException(422, "请输入大于 0 的有效价格")
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 5000:
        raise HTTPException(422, "请填写修改原因，最多 5000 字")
    reason = reason.strip()
    item, sku = (None, None) if basis == "reference" else scope(product, rows)
    if basis != "reference" and (not item or not sku):
        raise HTTPException(
            422, "尚无法确认 SKU，请先在商品链接中指定 skuId，或完成一次 SKU 核验采集"
        )
    # Prevent a background link change from silently moving an open editor to
    # another SKU. The UI submits the identifiers it originally displayed.
    if body.get("item_id") != item or body.get("sku_id") != sku:
        raise HTTPException(409, "商品链接或当前 SKU 已变化，请刷新后重试")
    version = body.get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise HTTPException(422, "无效版本号")
    platform = product_identity(product["url"])["platform"]
    entity_id = key(product["id"], item, sku, basis, text, platform)
    values = {
        "product_id": product["id"],
        "item_id": item,
        "platform": platform,
        "sku_id": sku,
        "reference_product_id": product["id"] if basis == "reference" else None,
        "series_key": (
            pd.reference_key(product["id"])
            if basis == "reference"
            else pd.identity({"platform": platform, "item_id": item, "requested_sku_id": sku, "price_basis": basis})
        ),
        "basis": basis,
        "day": text,
        "price": price,
        "reason": reason,
        "product_context": {
            k: product.get(k, "")
            for k in ("category", "brand", "model", "spec", "shop_type")
        },
    }
    stamp = s.now()
    with s.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        before = s.decode(
            db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        )
        expected = before["version"] if before and not before["deleted_at"] else 0
        if expected != version:
            raise HTTPException(409, "该日价格已被其他操作修改，请刷新后重试")
        db.execute(
            "INSERT INTO entities VALUES(?,?,?,1,NULL,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,version=version+1,deleted_at=NULL,updated_at=excluded.updated_at",
            (entity_id, "daily_price", s.dumps(values), stamp, stamp),
        )
        after = s.decode(
            db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        )
        s.audit(
            db,
            entity_id,
            "daily_price",
            "update" if expected else "create",
            before,
            after,
            reason,
        )
    return after
