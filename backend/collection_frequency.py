"""Atomically set the automatic collection period for all active products."""

import math
from datetime import timedelta

from fastapi import HTTPException

from . import domain as d, store as s


def apply(hours):
    if (
        isinstance(hours, bool)
        or not isinstance(hours, (int, float))
        or not math.isfinite(hours)
        or not 0.1 <= hours <= 8760
    ):
        raise HTTPException(422, "自动采集周期必须是 0.1–8760 小时的数字")
    stamp = s.now()
    updated = 0
    with s.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        products = [
            s.decode(row)
            for row in db.execute(
                "SELECT * FROM entities WHERE kind='product' AND deleted_at IS NULL"
            )
        ]
        for before in products:
            if before["interval_hours"] == hours:
                continue
            next_run = None
            if before.get("url") and not before["paused"]:
                last = (
                    d.dt(before["last_scheduled"])
                    if before.get("last_scheduled")
                    else d.utcnow()
                )
                next_run = max(d.utcnow(), last + timedelta(hours=hours)).isoformat()
            after = {
                **before,
                "interval_hours": hours,
                "next_run": next_run,
                "version": before["version"] + 1,
                "updated_at": stamp,
            }
            db.execute(
                "UPDATE entities SET data=?,version=?,updated_at=? WHERE id=?",
                (s.dumps(s.payload(after)), after["version"], stamp, before["id"]),
            )
            # This harmless schedule edit must not cancel a paused product's
            # explicitly requested one-shot collection.
            db.execute(
                "UPDATE jobs SET context=json_set(context,'$.pause_version',?) WHERE product_id=? AND status IN ('queued','running') AND json_extract(context,'$.allow_paused')=1 AND json_extract(context,'$.pause_version')=?",
                (after["version"], before["id"], before["version"]),
            )
            s.audit(
                db,
                before["id"],
                "product",
                "update",
                before,
                after,
                "统一修改自动采集周期",
            )
            updated += 1
    s.log(
        f"统一采集周期：共 {len(products)} 件商品，更新 {updated} 件，每 {hours:g} 小时采集；暂停状态保留"
    )
    return {"total": len(products), "updated": updated, "interval_hours": hours}
