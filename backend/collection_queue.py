"""User-visible queue reasons, without exposing another user's tasks."""

import threading

from . import collection_batches as batches, collector, store as s, price_sources
from . import domain as d

_LOCK = threading.RLock()
_SLOTS = {}


def publish(pools):
    with _LOCK:
        _SLOTS[str(s.DATA)] = {owner: sum(not task.done() for task in pool.tasks) for owner, pool in pools.items()}


def clear():
    with _LOCK:
        _SLOTS.pop(str(s.DATA), None)


def snapshot():
    config = s.settings()
    owner = s.TENANT.get()
    with _LOCK:
        slots = _SLOTS.get(str(s.DATA), {}).copy()
    with s.connection() as db:
        own_running = db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]
        draining = bool(db.execute("SELECT 1 FROM jobs WHERE status='running' AND json_extract(context,'$.source_revision') IS NOT NULL AND json_extract(context,'$.source_revision')<>? LIMIT 1", (config.get("price_source_revision", 0),)).fetchone())
        queued = [dict(row) for row in db.execute(f"SELECT id,created_at,context FROM jobs WHERE status='queued' ORDER BY {batches.QUEUE_ORDER}")]
    if config["global_paused"]:
        queued.sort(key=lambda row: not batches.is_manual(row))
    return {"config": config, "owner": owner, "slots": slots, "own_running": own_running, "draining": draining,
        "first": queued[0] if queued else None, "positions": {row["id"]: i + 1 for i, row in enumerate(queued)}}


def explain(job=None, context=None):
    context = context or snapshot()
    config, owner, slots, own_running = (context[key] for key in ("config", "owner", "slots", "own_running"))
    used = max(own_running, slots.get(owner, 0))
    normal_used = sum(value for key, value in slots.items() if key != "admin")
    selected = job or context["first"]
    verification = selected and batches.context(selected).get("verify_session")
    manual = bool(selected and batches.is_manual(selected))
    blocks = []
    def block(code, message, until=None):
        blocks.append({"code": code, "message": message, "resume_at": until})
    if context.get("draining"):
        block("source_switch", "价格来源已切换，等待旧来源的在途任务收尾后执行新来源")
    browser = price_sources.mode(config) == "crawler"
    if not manual and browser and not collector.runtime().SESSION.is_file() and owner != "admin":
        block("cookie_missing", "等待上传当前账号自己的淘宝 Cookie")
    if not manual and browser and collector.runtime().LOGIN["status"] in ("opening", "waiting"):
        block("login_open", "等待当前账号的淘宝登录操作完成")
    if not manual:
        if config["global_paused"]:
            block("paused", "定时采集已暂停，定时任务等待恢复；手动采集不受影响")
        if browser and collector.runtime().LOGIN["status"] == "expired":
            if collector.runtime().LOGIN.get("issue") == "verification_required":
                block("verification_required", "当前账号遇到淘宝人工验证，自动采集已暂停；手动采集仍可执行")
            else:
                block("session_expired", "当前账号淘宝登录失效，请更新 Cookie 或校验登录状态")
        if config["circuit_until"] and d.dt(config["circuit_until"]) > d.utcnow():
            block("circuit", "当前账号自动采集正在熔断冷却；手动采集仍可执行", config["circuit_until"])
    if selected:
        options = batches.context(selected)
        if options.get("batch_id") and not verification:
            with s.connection() as db:
                pending = db.execute("SELECT 1 FROM jobs WHERE json_extract(context,'$.batch_id')=? AND json_extract(context,'$.wave')<? AND status IN ('queued','running') LIMIT 1", (options["batch_id"], options["wave"])).fetchone()
                until = batches.cooling_until(db, options["batch_id"], options["wave"])
                if not until and options["wave"]:
                    until = batches.cooling_until(db, options["batch_id"], options["wave"] - 1)
            if pending:
                block("previous_batch", "等待前一批采集完成")
            elif until and d.dt(until) > d.utcnow():
                block("batch_cooldown", "等待本账号批次间隔结束", until)
    limit = min(5, config["max_concurrency"])
    if used >= limit:
        block("user_capacity", "当前账号的并发名额已满，任务完成或间隔结束后继续")
    if owner != "admin" and normal_used >= 50:
        block("server_capacity", "普通用户共享的 50 个名额已满，释放后自动继续")
    if not blocks:
        block("queued" if selected else "idle", "已在队列中，等待调度" if selected else "没有等待中的任务")
    since = selected.get("created_at") if selected else None
    return {"reason": blocks[0]["code"], "message": blocks[0]["message"], "blocks": blocks,
        "waiting_since": since, "wait_seconds": max(0, int((d.utcnow() - d.dt(since)).total_seconds())) if since else 0,
        "position": context["positions"].get(selected["id"]) if selected else None,
        "own_slots": used, "own_limit": limit, "shared_limit": None if owner == "admin" else 50}
