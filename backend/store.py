"""Local, transactional persistence. Raw observations are append-only."""

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("MONITOR_DATA_DIR", ROOT / "data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
os.chmod(DATA, 0o700)
DB = DATA / "monitor.sqlite3"
LOCK = threading.RLock()
REVISION = 0
TENANT = ContextVar("monitor_tenant", default="admin")


@contextmanager
def tenant_scope(user_id):
    if user_id != "admin" and not re.fullmatch(r"[a-f0-9]{32}", user_id):
        raise ValueError("无效的用户标识")
    token = TENANT.set(user_id)
    try:
        yield
    finally:
        TENANT.reset(token)


def data_dir():
    user_id = TENANT.get()
    return DATA if user_id == "admin" else DATA / "users" / user_id


def db_path():
    return DB if TENANT.get() == "admin" else data_dir() / "monitor.sqlite3"


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def connection():
    global REVISION
    with LOCK:
        db = sqlite3.connect(db_path(), timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=20000")
        try:
            yield db
            db.commit()
            if db.total_changes:
                REVISION += 1
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


DEFAULTS = {
    "price_source": "crawler",
    "price_source_revision": 0,
    "price_apis": {},
    "theme": "light",
    "global_paused": False,
    "browser": "msedge",
    "crawler_headed": False,
    "crawler_start_gap": 5,
    "wait_seconds": 12,
    "request_gap": 8,
    "max_retries": 1,
    "max_concurrency": 5,
    "circuit_failures": 5,
    "circuit_hours": 1,
    "circuit_until": None,
    "failures": 0,
    "confirm_count": 2,
    "anomaly_percent": 40,
    "delist_count": 3,
    "delist_hours": 24,
    "log_days": 30,
    "trash_days": 30,
    "observation_years": 2,
    "backup_hour": 3,
    "backup_keep": 7,
    "last_backup": None,
    "quiet_start": "23:00",
    "quiet_end": "08:00",
    "digest_minutes": 0,
    "include_coupon": False,
    "trusted_only": True,
    "event_boost": False,
    "event_boost_days": 3,
    "event_interval_minutes": 30,
    "default_range": 30,
    "forecast_days": 14,
    "min_days": 30,
    "min_changes": 3,
    "low_percentile": 20,
    "drop_threshold": 3,
    "shadow_mode": True,
    "ml_enabled": True,
    "ml_auto_train": True,
    "channels": {},
    "dashboard_cards": ["watch", "distribution", "drops", "alerts", "health", "events"],
    "card_sizes": {},
    "default_view": None,
    "chart_step": True,
    "chart_layers": ["sale", "original", "manual", "events"],
    "density": "middle",
}


def init():
    data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir().chmod(0o700)
    with connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS entities(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, deleted_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS entities_kind ON entities(kind,deleted_at);
        CREATE TABLE IF NOT EXISTS observations(
          id TEXT PRIMARY KEY, product_id TEXT NOT NULL, observed_at TEXT NOT NULL,
          price INTEGER, original_price INTEGER, stock TEXT NOT NULL,
          status TEXT NOT NULL, data TEXT NOT NULL, job_id TEXT UNIQUE);
        CREATE INDEX IF NOT EXISTS obs_product_time ON observations(product_id,observed_at);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, product_id TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
          attempts INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_active ON jobs(product_id)
          WHERE status IN ('queued','running');
        CREATE INDEX IF NOT EXISTS jobs_finished ON jobs(finished_at,id)
          WHERE status IN ('success','failed');
        CREATE TABLE IF NOT EXISTS audit(
          id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
          action TEXT NOT NULL, before_data TEXT, after_data TEXT,
          reason TEXT NOT NULL, created_at TEXT NOT NULL, reverted_at TEXT);
        CREATE INDEX IF NOT EXISTS audit_time ON audit(created_at);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs(
          id TEXT PRIMARY KEY, product_id TEXT, level TEXT NOT NULL,
          message TEXT NOT NULL, created_at TEXT NOT NULL, duration_ms INTEGER);
        CREATE TABLE IF NOT EXISTS deliveries(
          id TEXT PRIMARY KEY, rule_id TEXT, product_id TEXT, channel TEXT NOT NULL,
          message TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          sent_at TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          dedup_key TEXT UNIQUE, next_attempt TEXT);
        CREATE TABLE IF NOT EXISTS alert_states(
          key TEXT PRIMARY KEY, active INTEGER NOT NULL, last_sent TEXT, signature TEXT);
        CREATE TABLE IF NOT EXISTS forecasts(
          id TEXT PRIMARY KEY, product_id TEXT NOT NULL, created_at TEXT NOT NULL,
          data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS forecast_product ON forecasts(product_id,created_at);
        CREATE TABLE IF NOT EXISTS model_runs(
          id TEXT PRIMARY KEY, created_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, report TEXT NOT NULL);
        PRAGMA user_version=1;
        """)
        if "details" not in {r["name"] for r in db.execute("PRAGMA table_info(logs)")}:
            db.execute("ALTER TABLE logs ADD COLUMN details TEXT")
        if "context" not in {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}:
            db.execute("ALTER TABLE jobs ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
        db.execute(
            "CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(json_extract(context,'$.batch_id'),json_extract(context,'$.wave'),status)"
        )
        for key, value in DEFAULTS.items():
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES(?,?)", (key, dumps(value))
            )
    if db_path().exists():
        os.chmod(db_path(), 0o600)


def settings():
    with connection() as db:
        return {
            r["key"]: json.loads(r["value"])
            for r in db.execute("SELECT * FROM settings")
        }


def set_settings(values):
    with connection() as db:
        for key, value in values.items():
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?)", (key, dumps(value))
            )


def decode(row):
    if not row:
        return None
    r = dict(row)
    return {
        **json.loads(r["data"]),
        **{
            k: r[k]
            for k in ("id", "kind", "version", "deleted_at", "created_at", "updated_at")
        },
    }


def get(entity_id, kind=None, include_deleted=False):
    with connection() as db:
        result = decode(
            db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        )
    if (
        not result
        or (kind and result["kind"] != kind)
        or (result["deleted_at"] and not include_deleted)
    ):
        return None
    return result


def entities(kind, deleted=False):
    with connection() as db:
        rows = db.execute(
            "SELECT * FROM entities WHERE kind=? AND deleted_at IS "
            + ("NOT NULL" if deleted else "NULL")
            + " ORDER BY created_at DESC",
            (kind,),
        ).fetchall()
    return [decode(r) for r in rows]


META = {"id", "kind", "version", "deleted_at", "created_at", "updated_at"}


def payload(entity):
    return {k: v for k, v in entity.items() if k not in META}


def audit(db, entity_id, kind, action, before, after, reason=""):
    db.execute(
        "INSERT INTO audit VALUES(?,?,?,?,?,?,?,?,NULL)",
        (
            uid(),
            entity_id,
            kind,
            action,
            dumps(before) if before else None,
            dumps(after) if after else None,
            reason,
            now(),
        ),
    )


def create(kind, data, reason="", entity_id=None, audited=True):
    stamp, entity_id = now(), entity_id or uid()
    with connection() as db:
        db.execute(
            "INSERT INTO entities VALUES(?,?,?,1,NULL,?,?)",
            (entity_id, kind, dumps(data), stamp, stamp),
        )
        result = decode(
            db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        )
        if audited:
            audit(db, entity_id, kind, "create", None, result, reason)
    return result


def update(
    entity_id, changes, version=None, reason="", audited=True, deleted_at="unchanged"
):
    with connection() as db:
        before = decode(
            db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        )
        if not before:
            raise KeyError("记录不存在")
        if version is not None and before["version"] != version:
            raise ValueError("记录已被其他操作修改，请刷新后重试")
        data = {
            **payload(before),
            **{k: v for k, v in changes.items() if k not in META},
        }
        deleted = before["deleted_at"] if deleted_at == "unchanged" else deleted_at
        db.execute(
            "UPDATE entities SET data=?,version=version+1,updated_at=?,deleted_at=? WHERE id=?",
            (dumps(data), now(), deleted, entity_id),
        )
        after = decode(
            db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
        )
        if audited:
            audit(
                db,
                entity_id,
                before["kind"],
                (
                    "delete"
                    if deleted
                    else ("restore" if before["deleted_at"] else "update")
                ),
                before,
                after,
                reason,
            )
    return after


def log(message, product_id=None, level="info", duration_ms=None, details=None):
    with connection() as db:
        db.execute(
            "INSERT INTO logs(id,product_id,level,message,created_at,duration_ms,details) VALUES(?,?,?,?,?,?,?)",
            (
                uid(),
                product_id,
                level,
                message,
                now(),
                duration_ms,
                dumps(details) if details else None,
            ),
        )


def enqueue(product_id, manual=False):
    with connection() as db:
        old = db.execute(
            "SELECT id FROM jobs WHERE product_id=? AND status IN ('queued','running')",
            (product_id,),
        ).fetchone()
        if old:
            if manual:
                db.execute("UPDATE jobs SET context=json_set(context,'$.manual',1) WHERE id=? AND status IN ('queued','running')", (old["id"],))
            return old["id"]
        job_id = uid()
        db.execute(
            "INSERT INTO jobs(id,product_id,status,created_at,context) VALUES(?,?,?,?,?)",
            (job_id, product_id, "queued", now(), dumps({"manual": True} if manual else {})),
        )
        return job_id


def permanent_delete(entity_id):
    item = get(entity_id, include_deleted=True)
    if not item or not item["deleted_at"]:
        raise ValueError("只能永久删除回收站中的记录")
    with connection() as db:
        if item["kind"] == "product":
            db.execute(
                "DELETE FROM entities WHERE kind='change' AND id IN (SELECT 'change_'||id FROM observations WHERE product_id=?)",
                (entity_id,),
            )
            db.execute("DELETE FROM observations WHERE product_id=?", (entity_id,))
            db.execute("DELETE FROM jobs WHERE product_id=?", (entity_id,))
            db.execute("DELETE FROM forecasts WHERE product_id=?", (entity_id,))
            db.execute(
                "DELETE FROM entities WHERE kind IN ('note','correction','daily_price') AND json_extract(data,'$.product_id')=?",
                (entity_id,),
            )
            db.execute(
                "UPDATE deliveries SET status='cancelled' WHERE product_id=? AND status IN ('pending','retry')",
                (entity_id,),
            )
        db.execute("DELETE FROM entities WHERE id=?", (entity_id,))
        audit(db, entity_id, item["kind"], "purge", item, None, "永久删除")


def system_notice(message):
    config = settings()
    channels = ["inapp"] + [
        key for key, value in config["channels"].items() if value.get("enabled")
    ]
    with connection() as db:
        for channel in channels:
            db.execute(
                "INSERT INTO deliveries(id,channel,message,status,created_at) VALUES(?,?,?,?,?)",
                (
                    uid(),
                    channel,
                    message,
                    "sent" if channel == "inapp" else "pending",
                    now(),
                ),
            )
