"""Durable one-shot collection waves; ordinary monitoring keeps its own pacing."""

import json
from datetime import datetime, timedelta

from . import collector, store as s, price_sources

BATCH_SIZE = 5
GAP_SECONDS = 8
MANUAL_SQL = "(json_extract(context,'$.manual')=1 OR json_extract(context,'$.batch_id') IS NOT NULL OR json_extract(context,'$.verify_session')=1)"
QUEUE_ORDER = f"CASE WHEN json_extract(context,'$.verify_session')=1 THEN 0 WHEN {MANUAL_SQL} THEN 1 ELSE 2 END, coalesce(json_extract(context,'$.batch_created_at'),created_at), coalesce(json_extract(context,'$.wave'),0),created_at,id"


def context(job):
    return json.loads(job.get("context") or "{}")


def is_manual(job):
    options = context(job)
    return bool(options.get("manual") or options.get("batch_id") or options.get("verify_session"))


def permitted_product(job, product):
    if not product or not product.get("url"):
        return False
    # Product pause controls its timer. Explicit manual work remains permitted;
    # deleted products and explicit task cancellation still take effect.
    return is_manual(job) or not product["paused"]


def cooling_until(db, batch_id, wave):
    row = db.execute(
        "SELECT max(coalesce(json_extract(context,'$.released_at'),finished_at)) "
        "FROM jobs WHERE json_extract(context,'$.batch_id')=? "
        "AND json_extract(context,'$.wave')=? AND status NOT IN ('queued','running')",
        (batch_id, wave),
    ).fetchone()
    return (
        (datetime.fromisoformat(row[0]) + timedelta(seconds=GAP_SECONDS)).isoformat()
        if row[0]
        else None
    )


def ready(db, batch_id, wave):
    if db.execute(
        "SELECT 1 FROM jobs WHERE json_extract(context,'$.batch_id')=? "
        "AND json_extract(context,'$.wave')<? AND status IN ('queued','running') LIMIT 1",
        (batch_id, wave),
    ).fetchone():
        return False
    until = cooling_until(db, batch_id, wave - 1) if wave else None
    return not until or datetime.fromisoformat(s.now()) >= datetime.fromisoformat(until)


def summary(db, batch_id):
    rows = db.execute(
        "SELECT status,context,finished_at FROM jobs WHERE json_extract(context,'$.batch_id')=?",
        (batch_id,),
    ).fetchall()
    if not rows:
        return None
    running = sum(row["status"] == "running" for row in rows)
    waiting = sum(row["status"] == "queued" for row in rows)
    queued_waves = [
        json.loads(row["context"])["wave"] for row in rows if row["status"] == "queued"
    ]
    next_at = None
    if queued_waves and not running:
        wave = min(queued_waves)
        # The same wave can be split if the concurrency setting is lowered.
        next_at = cooling_until(db, batch_id, wave) or (
            cooling_until(db, batch_id, wave - 1) if wave else None
        )
        if next_at and datetime.fromisoformat(next_at) <= datetime.fromisoformat(
            s.now()
        ):
            next_at = None
    return {
        "id": batch_id,
        "total": len(rows),
        "completed": len(rows) - running - waiting,
        "running": running,
        "waiting": waiting,
        "batch_size": json.loads(rows[0]["context"]).get("batch_size", BATCH_SIZE),
        "gap_seconds": GAP_SECONDS,
        "next_batch_at": next_at,
    }


def active_summary():
    with s.connection() as db:
        row = db.execute(
            "SELECT json_extract(context,'$.batch_id') AS batch_id FROM jobs "
            "WHERE status IN ('queued','running') AND json_extract(context,'$.batch_id') IS NOT NULL "
            "ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        return summary(db, row["batch_id"]) if row else None


def enqueue_all():
    config = s.settings()
    batch_size = min(BATCH_SIZE, max(1, int(config["max_concurrency"])))
    with s.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT json_extract(context,'$.batch_id') AS batch_id FROM jobs "
            "WHERE status IN ('queued','running') AND json_extract(context,'$.batch_id') IS NOT NULL "
            "ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        if active:
            return {
                "already_active": True,
                "added": 0,
                "batch": summary(db, active["batch_id"]),
            }
        products = [
            s.decode(row)
            for row in db.execute(
                "SELECT * FROM entities WHERE kind='product' AND deleted_at IS NULL ORDER BY created_at,id"
            )
        ]
        active_products = {
            row["product_id"]: dict(row)
            for row in db.execute(
                "SELECT id,product_id,status FROM jobs WHERE status IN ('queued','running')"
            )
        }
        missing = invalid = existing = unsupported = 0
        batch_id = s.uid()
        batch_created_at = s.now()
        added = 0
        reused = 0
        for product in products:
            if not product.get("url"):
                missing += 1
                continue
            try:
                collector.normalize_url(product["url"])
            except (ValueError, TypeError, AttributeError):
                invalid += 1
                continue
            if not price_sources.supported(product, config):
                unsupported += 1
                continue
            old = active_products.get(product["id"])
            if old and old["status"] == "running":
                db.execute("UPDATE jobs SET context=json_set(context,'$.manual',1) WHERE id=?", (old["id"],))
                existing += 1
                continue
            options = {
                "manual": True,
                "batch_id": batch_id,
                "batch_created_at": batch_created_at,
                "wave": (added + reused) // batch_size,
                "batch_size": batch_size,
                "allow_paused": bool(product["paused"]),
                "pause_version": product["version"],
            }
            if old:
                db.execute(
                    "UPDATE jobs SET context=? WHERE id=? AND status='queued'",
                    (s.dumps(options), old["id"]),
                )
                reused += 1
            else:
                db.execute(
                    "INSERT INTO jobs(id,product_id,status,created_at,context) VALUES(?,?,'queued',?,?)",
                    (s.uid(), product["id"], s.now(), s.dumps(options)),
                )
                added += 1
        batch = summary(db, batch_id) if added + reused else None
    s.log(
        f"一键采集：新增 {added} 个任务、复用 {reused} 个排队任务、已有 {existing} 个任务运行，跳过 {missing} 个无链接商品、{invalid} 个无效链接；每批最多 {batch_size} 个，批次间隔 {GAP_SECONDS} 秒"
    )
    return {
        "already_active": False,
        "added": added,
        "reused": reused,
        "existing": existing,
        "skipped_no_url": missing,
        "skipped_invalid_url": invalid,
        "skipped_source": unsupported,
        "batch": batch,
    }
