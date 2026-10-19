import asyncio
import json
import random
import shutil
import sqlite3
import time
from datetime import timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from diagnostics import describe, exception_detail

from . import accounts, collection_batches as batches, collector, prediction, training_queue, collection_queue, session_verification, price_sources, official_api, crawl_pacing, outbound
from . import domain as d
from . import store as s

MAX_CONCURRENCY = 5
MAX_ORDINARY_CONCURRENCY = 50
POLL_SECONDS = 0.25


def backup():
    folder = s.data_dir() / "backups"
    folder.mkdir(exist_ok=True)
    stamp = d.utcnow().strftime("%Y%m%d-%H%M%S-%f")
    path = folder / f"monitor-{stamp}.sqlite3"
    with s.connection() as db:
        target = sqlite3.connect(path)
        try:
            db.backup(target)
            target.execute("PRAGMA secure_delete=ON")
            target.execute("UPDATE settings SET value='{}' WHERE key='channels'")
            target.execute("UPDATE settings SET value='{}' WHERE key='price_apis'")
            target.execute("UPDATE settings SET value='\"crawler\"' WHERE key='price_source'")
            target.commit()
            target.execute("VACUUM")
        finally:
            target.close()
    path.chmod(0o600)
    s.set_settings({"last_backup": s.now()})
    for old in sorted(folder.glob("monitor-*.sqlite3"), reverse=True)[
        s.settings()["backup_keep"] :
    ]:
        old.unlink()
    s.log("数据库备份完成（不含 Cookie 与会话文件）")
    return path.name


def quiet(config):
    start, end = config["quiet_start"], config["quiet_end"]
    if not start or not end or start == end:
        return False
    current = d.utcnow().astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    return start <= current < end if start < end else current >= start or current < end


async def send(channel, text, config):
    channel_config = config.get("channels", {}).get(channel, {})
    if channel == "inapp":
        return
    if not channel_config.get("enabled"):
        raise ValueError("渠道未启用或未配置")
    outbound.validate_config(channel, channel_config)
    if channel == "email":
        msg = EmailMessage()
        msg["Subject"] = "鬼打架 · 硬件价格提醒"
        msg["From"] = channel_config["user"]
        msg["To"] = channel_config["to"]
        msg.set_content(text)
        await outbound.send_email(channel_config, msg)
        return
    url = channel_config.get("url", "")
    if channel == "bark":
        payload = {"json": {"title": "鬼打架 · 价格提醒", "body": text, "group": "硬件价格"}}
    elif channel == "serverchan":
        payload = {"data": {"title": "鬼打架价格提醒", "desp": text}}
    elif channel == "wecom":
        payload = {"json": {"msgtype": "text", "text": {"content": text}}}
    elif channel == "feishu":
        payload = {"json": {"msg_type": "text", "content": {"text": text}}}
    else:
        raise ValueError("不支持的渠道")
    result = await outbound.post_json(url, **payload)
    if result.get("errcode", 0) != 0 or result.get("code", 0) not in (0, 200):
        raise ValueError("渠道返回失败，请检查地址或密钥")


async def process_deliveries():
    config = s.settings()
    if quiet(config):
        return
    with s.connection() as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM deliveries WHERE status IN ('pending','retry') AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY created_at LIMIT 10",
                (s.now(),),
            )
        ]
    for row in rows:
        if row["product_id"] and not s.get(row["product_id"], "product"):
            with s.connection() as db:
                db.execute(
                    "UPDATE deliveries SET status='cancelled' WHERE id=?", (row["id"],)
                )
            continue
        try:
            await send(row["channel"], row["message"], config)
            with s.connection() as db:
                db.execute(
                    "UPDATE deliveries SET status='sent',sent_at=?,error=NULL,attempts=attempts+1 WHERE id=?",
                    (s.now(), row["id"]),
                )
        except Exception as exc:
            attempts = row["attempts"] + 1
            error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            with s.connection() as db:
                db.execute(
                    "UPDATE deliveries SET status=?,error=?,attempts=?,next_attempt=? WHERE id=?",
                    (
                        "failed" if attempts >= 3 else "retry",
                        error,
                        attempts,
                        (d.utcnow() + timedelta(minutes=2**attempts)).isoformat(),
                        row["id"],
                    ),
                )


def schedule():
    config = s.settings()
    if not account_cookie_ready(config) or config["global_paused"] or (price_sources.mode(config) == "crawler" and collector.runtime().LOGIN["status"] in (
        "opening",
        "waiting",
        "expired",
    )):
        return
    if config["circuit_until"] and d.dt(config["circuit_until"]) > d.utcnow():
        return
    events = s.entities("event") if config["event_boost"] else []
    for product in s.entities("product"):
        if product["paused"] or not product.get("url"):
            continue
        if not price_sources.supported(product, config):
            continue
        interval = timedelta(hours=product["interval_hours"])
        for event in events:
            if (
                event["confidence"] == "官宣"
                and not event.get("ignored")
                and d.event_applies(event, product)
            ):
                start = d.dt(event["start"]) - timedelta(
                    days=config["event_boost_days"]
                )
                end = d.dt(event["end"]) + timedelta(
                    days=config["event_boost_days"] + 1
                )
                if start <= d.utcnow() <= end:
                    interval = min(
                        interval, timedelta(minutes=config["event_interval_minutes"])
                    )
        last = product.get("last_scheduled")
        if not last or d.utcnow() - d.dt(last) >= interval:
            s.enqueue(product["id"])
            s.update(
                product["id"],
                {
                    "last_scheduled": s.now(),
                    "next_run": (d.utcnow() + interval).isoformat(),
                },
                audited=False,
            )


def refresh_job_context(job):
    with s.connection() as db:
        latest = db.execute("SELECT context FROM jobs WHERE id=?", (job["id"],)).fetchone()
    if latest:
        job["context"] = latest[0]


async def run_job(job):
    # Re-read context: a queued collection may have been selected for Cookie verification.
    refresh_job_context(job)
    verification = batches.context(job).get("verify_session")
    config = job.get("_source_config") or s.settings()
    source = price_sources.mode(config)
    if source == "api":
        verification = False
        if batches.context(job).get("purpose") == "cookie_verification":
            with s.connection() as db:
                db.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?", (s.now(), job["id"]))
            return
    product = session_verification.target(job) or s.get(job["product_id"], "product")
    if not batches.permitted_product(job, product):
        with s.connection() as db:
            db.execute(
                "UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?",
                (s.now(), job["id"]),
            )
        return
    with s.connection() as db:
        db.execute(
            "UPDATE jobs SET status='running',started_at=? WHERE id=?",
            (s.now(), job["id"]),
        )
        db.execute("UPDATE jobs SET context=json_set(context,'$.price_source',?,'$.source_revision',?) WHERE id=?", (source, config.get("price_source_revision", 0), job["id"]))
    start = time.monotonic()
    verification_generation = collector.session_state.generation(collector.runtime().SESSION)
    result = {"price": None, "status": "crawler_error", "stock": "unknown"}
    attempt_history = []
    remaining_budget = collector.COLLECT_TIMEOUT_SECONDS
    for attempt in range((0 if verification else config["max_retries"]) + 1):
        if remaining_budget <= 0:
            result = collector.timeout_result_with_progress(result)
            break
        current = session_verification.target(job) or s.get(product["id"], "product")
        if not batches.permitted_product(job, current):
            with s.connection() as db:
                db.execute(
                    "UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?",
                    (s.now(), job["id"]),
                )
            return
        pacing_wait = 0
        if source == "crawler":
            def ready():
                # Clicking manual collection also promotes an already running
                # scheduled task that is still waiting for its browser turn.
                refresh_job_context(job)
                latest = s.settings()
                if (price_sources.mode(latest) != source
                        or latest.get("price_source_revision", 0) != config.get("price_source_revision", 0)):
                    return False
                return permitted(latest, manual=batches.is_manual(job))

            pacing_wait = await crawl_pacing.wait_turn(job, config["crawler_start_gap"], ready)
            if pacing_wait is None:
                with s.connection() as db:
                    db.execute("UPDATE jobs SET status='queued',started_at=NULL WHERE id=? AND status='running'", (job["id"],))
                return
            verification_generation = collector.session_state.generation(collector.runtime().SESSION)
            session_verification.begin(collector.runtime().SESSION, job["id"])
            with s.connection() as db:
                db.execute("UPDATE jobs SET started_at=? WHERE id=?", (s.now(), job["id"]))
        attempt_started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                # collector owns the 120s browser deadline; leave a small cleanup
                # grace window so a detected login redirect is not relabeled by
                # the worker while its child process is being reaped.
                official_api.collect(product, config) if source == "api" else collector.collect(product), timeout=remaining_budget + 2
            )
        except asyncio.TimeoutError:
            result = collector.timeout_result()
        finally:
            remaining_budget -= time.monotonic() - attempt_started
        refresh_job_context(job)
        if source == "crawler":
            result.setdefault("diagnostics", {}).update(
                browser_start_wait_ms=pacing_wait, crawler_start_gap=config["crawler_start_gap"],
                browser_headed=config["crawler_headed"],
            )
        attempt_history.append(
            {
                "attempt": attempt + 1,
                "status": result["status"],
                "diagnostics": describe(result),
            }
        )
        with s.connection() as db:
            db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?", (job["id"],))
        if result["price"] is not None or result["status"] in (
            "blocked",
            "login_or_redirect",
            "sku_unverified",
            "sku_mismatch",
            "price_masked",
            "timeout",
            "api_not_configured", "api_auth_error", "api_permission_error", "api_rate_limit",
            "api_sku_required", "api_sku_unsupported", "api_empty", "api_response_invalid", "source_unsupported",
        ):
            break
        if attempt < config["max_retries"]:
            retry_wait = min(config["request_gap"] * (attempt + 1), max(0, remaining_budget))
            await asyncio.sleep(retry_wait)
            remaining_budget -= retry_wait
    result["diagnostics"] = describe(result)
    result["diagnostics"]["attempts"] = attempt_history
    verification_state = session_verification.finish(collector.runtime().SESSION, job["id"], result, verification_generation) if source == "crawler" else None
    if (source == "crawler" and result["status"] in ("blocked", "login_or_redirect")
            and verification_generation == collector.session_state.generation(collector.runtime().SESSION)
            and collector.runtime().LOGIN["status"] not in ("opening", "waiting")):
        verification_state = "verification_required" if result["status"] == "blocked" else "login_required"
        message = ("淘宝要求人工验证，自动采集已暂停；手动采集仍可执行，请完成验证并保存会话"
                   if verification_state == "verification_required" else "当前会话跳转登录页，请更新 Cookie 后重新校验")
        session_verification.write(collector.runtime().SESSION, {
            "state": verification_state, "message": message,
            "job_id": job["id"], "code": result["status"], "generation": verification_generation,
            "updated_at": s.now(), "checked_at": s.now(), "diagnostics": describe(result),
        })
    if verification_state == "passed":
        collector.runtime().LOGIN.update(status="saved", message="Cookie 实际校验通过", issue=None)
    elif verification_state in ("login_required", "verification_required"):
        collector.runtime().LOGIN.update(status="expired", message=(
            "淘宝要求人工验证，自动采集已暂停；手动采集仍可执行，请完成验证并保存会话"
            if verification_state == "verification_required" else "登录状态需要更新，请上传 Cookie 或重新登录后再校验"), issue=verification_state)
    if batches.context(job).get("purpose") == "cookie_verification":
        success = result["status"] == "success" and result.get("price") is not None
        with s.connection() as db:
            db.execute("UPDATE jobs SET status=?,finished_at=?,error=?,result=? WHERE id=?", ("success" if success else "failed", s.now(), None if success else result["status"], s.dumps(result), job["id"]))
        s.log("Cookie 商品校验完成" if success else "Cookie 商品校验未通过或无法确认", job["product_id"], "info" if success else "warning", details=result["diagnostics"])
        return
    result["product_context"] = {
        key: product.get(key, "")
        for key in ("category", "brand", "model", "spec", "shop_type", "release_date")
    }
    # A delete/pause performed while the browser was running must take effect.
    current = s.get(product["id"], "product")
    if not batches.permitted_product(job, current):
        with s.connection() as db:
            db.execute(
                "UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?",
                (s.now(), job["id"]),
            )
        return
    record_id = d.save_observation(product["id"], result, job["id"], source="api" if source == "api" else "crawler")
    success = result.get("price") is not None and result["status"] == "success"
    with s.connection() as db:
        stored = db.execute(
            "SELECT status FROM observations WHERE id=?", (record_id,)
        ).fetchone()["status"]
        success = success and stored == "success"
        result["diagnostics"] = describe({**result, "status": stored})
        db.execute(
            "UPDATE jobs SET status=?,finished_at=?,error=?,result=? WHERE id=?",
            (
                "success" if success else "failed",
                s.now(),
                None if success else stored,
                s.dumps(
                    {
                        "price": result["price"] if success else None,
                        "record_id": record_id,
                        "status": stored,
                        "diagnostics": result["diagnostics"],
                        "price_source": source,
                    }
                ),
                job["id"],
            ),
        )
    if not current.get("name") and result.get("title"):
        s.update(product["id"], {"name": result["title"]}, audited=False)
    rows = d.records(product["id"])
    with s.LOCK:
        latest_config = s.settings()
        same_source = latest_config.get("price_source_revision", 0) == config.get("price_source_revision", 0)
        same_session = source != "crawler" or verification_generation == collector.session_state.generation(collector.runtime().SESSION)
        if same_source and same_session and success and not batches.is_manual(job):
            s.set_settings({"failures": 0, "circuit_until": None})
        elif same_source and same_session and not batches.is_manual(job):
            failures = latest_config["failures"] + 1
            values = {"failures": failures}
            if failures >= latest_config["circuit_failures"]:
                values["circuit_until"] = (
                    d.utcnow() + timedelta(hours=latest_config["circuit_hours"])
                ).isoformat()
                if not latest_config["circuit_until"]:
                    s.log("采集连续失败，已进入熔断冷却", level="warning")
                    s.system_notice(
                        "鬼打架 · 价格采集连续失败，已自动进入熔断冷却，请查看采集管理。"
                    )
            s.set_settings(values)
    if success:
        d.evaluate_rules(current, rows)
    prediction = d.forecast(current, rows)
    with s.connection() as db:
        db.execute(
            "INSERT INTO forecasts VALUES(?,?,?,?)",
            (s.uid(), product["id"], s.now(), s.dumps(prediction)),
        )
    s.log(
        (
            "采集成功"
            if success
            else f"采集失败，价格 None（{stored}）：{describe({**result, 'status': stored})['message']}"
            + (
                f"；{result['diagnostics']['exception_message']}"
                if result["diagnostics"].get("exception_message")
                else ""
            )
        ),
        product["id"],
        "info" if success else "warning",
        int((time.monotonic() - start) * 1000),
        details={**result["diagnostics"], "job_id": job["id"], "code": stored},
    )


def maintenance():
    config = s.settings()
    local = d.utcnow().astimezone(ZoneInfo("Asia/Shanghai"))
    last = (
        d.dt(config["last_backup"]).astimezone(ZoneInfo("Asia/Shanghai"))
        if config["last_backup"]
        else None
    )
    if local.hour >= config["backup_hour"] and (
        last is None or last.date() < local.date()
    ):
        backup()
    if s.settings()["last_backup"]:
        cutoff = (d.utcnow() - timedelta(days=config["trash_days"])).isoformat()
        for kind in ("product", "rule", "event", "note", "view"):
            for item in s.entities(kind, deleted=True):
                if item["deleted_at"] < cutoff:
                    s.permanent_delete(item["id"])
    with s.connection() as db:
        db.execute(
            "DELETE FROM logs WHERE created_at<?",
            ((d.utcnow() - timedelta(days=config["log_days"])).isoformat(),),
        )
        # Keep retention explicit; old observations are purged only after a backup.
        if config["last_backup"]:
            db.execute(
                "DELETE FROM observations WHERE observed_at<?",
                (
                    (
                        d.utcnow() - timedelta(days=365 * config["observation_years"])
                    ).isoformat(),
                ),
            )
        db.execute(
            "DELETE FROM jobs WHERE status NOT IN ('queued','running') AND finished_at<?",
            ((d.utcnow() - timedelta(days=config["log_days"])).isoformat(),),
        )


def account_cookie_ready(config=None):
    # The server Mac's existing session belongs exclusively to the admin.
    # A normal account waits for its own uploaded session, never a fallback.
    return price_sources.mode(config) == "api" or s.TENANT.get() == "admin" or collector.runtime().SESSION.is_file()


def permitted(config, manual=False):
    # A manual command is an explicit attempt with the account's latest own
    # credentials. Missing/invalid credentials become a real task result instead
    # of leaving that command indefinitely queued behind automatic backoff.
    if manual:
        return True
    return (
        account_cookie_ready(config)
        and not config["global_paused"]
        and (price_sources.mode(config) == "api" or collector.runtime().LOGIN["status"] not in ("opening", "waiting", "expired"))
        and (not config["circuit_until"] or d.dt(config["circuit_until"]) <= d.utcnow())
    )


def claim_jobs(limit, allow_batch=True, verification_only=False, manual_only=False):
    if limit <= 0:
        return []
    with s.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM jobs WHERE status='queued' {'AND json_extract(context,\'$.verify_session\')=1' if verification_only else ''} {'AND ' + batches.MANUAL_SQL if manual_only else ''} ORDER BY {batches.QUEUE_ORDER} LIMIT ?",
                (limit,),
            )
        ]
        if rows:
            options = batches.context(rows[0])
            batch_id = options.get("batch_id")
            if batch_id:
                wave = options["wave"]
                if not allow_batch or (not options.get("verify_session") and not batches.ready(db, batch_id, wave)):
                    return []
                rows = [
                    row
                    for row in rows
                    if (
                        batches.context(row).get("batch_id"),
                        batches.context(row).get("wave"),
                    )
                    == (batch_id, wave)
                ]
            else:
                # Never mix a batch wave with standalone jobs. Queue ordering
                # already puts manual commands ahead of scheduled monitoring.
                first_batch = next(
                    (
                        i
                        for i, row in enumerate(rows)
                        if batches.context(row).get("batch_id")
                    ),
                    len(rows),
                )
                rows = rows[:first_batch]
        for row in rows:
            db.execute(
                "UPDATE jobs SET status='running',started_at=? WHERE id=? AND status='queued'",
                (s.now(), row["id"]),
            )
    return rows


async def execute_job(job):
    try:
        await run_job(job)
    except asyncio.CancelledError:
        # Restart resumes unfinished work; completed results stay completed.
        with s.connection() as db:
            db.execute(
                "UPDATE jobs SET status='queued',started_at=NULL WHERE id=? AND status='running'",
                (job["id"],),
            )
        raise
    except Exception as exc:
        details = exception_detail(exc, "worker")
        result = {"price": None, "status": "crawler_error", "diagnostics": details}
        with s.connection() as db:
            db.execute(
                "UPDATE jobs SET status='failed',finished_at=?,error='crawler_error',result=? WHERE id=? AND status='running'",
                (s.now(), s.dumps(result), job["id"]),
            )
        s.log(
            f"采集任务异常（{type(exc).__name__}），其他并发任务继续运行",
            job["product_id"],
            "warning",
            details=details,
        )
    if batches.context(job).get("batch_id"):
        with s.connection() as db:
            db.execute(
                "UPDATE jobs SET context=json_set(context,'$.released_at',?) WHERE id=? AND status NOT IN ('queued','running')",
                (s.now(), job["id"]),
            )
    elif not batches.is_manual(job):
        # Ordinary monitoring keeps its independent slot cooldown.
        await asyncio.sleep(s.settings()["request_gap"] + random.uniform(0, 2))


class CollectionPool:
    def __init__(self):
        self.tasks = {}
        self.active_wave = None
        self.source_key = None

    def dispatch(self, capacity=None):
        for task in tuple(self.tasks):
            if task.done():
                self.tasks.pop(task)
                if not task.cancelled():
                    task.result()
        config = s.settings()
        source_key = (price_sources.mode(config), config.get("price_source_revision", 0))
        # Drain the previous branch (including cancellation cleanup and cooldown)
        # before allowing any task from the newly selected branch to start.
        if self.tasks and self.source_key != source_key:
            return 0
        self.source_key = source_key
        if self.active_wave:
            if self.tasks:
                return 0
            with s.connection() as db:
                pending = db.execute(
                    "SELECT 1 FROM jobs WHERE json_extract(context,'$.batch_id')=? "
                    "AND status='queued' LIMIT 1", (self.active_wave[0],),
                ).fetchone()
                # The delay separates waves of the same batch. A completed or
                # cancelled batch must not postpone a later manual command.
                until = batches.cooling_until(db, *self.active_wave) if pending else None
            if until and d.dt(until) > d.utcnow():
                return 0
            self.active_wave = None
        limit = min(MAX_CONCURRENCY, max(1, int(config["max_concurrency"])))
        if capacity is not None:
            limit = min(limit, len(self.tasks) + max(0, capacity))
        jobs = claim_jobs(limit - len(self.tasks), allow_batch=not self.tasks, manual_only=not permitted(config))
        if jobs:
            options = batches.context(jobs[0])
            if options.get("batch_id"):
                self.active_wave = (options["batch_id"], options["wave"])
        for job in jobs:
            job["_source_config"] = config
            self.tasks[asyncio.create_task(execute_job(job))] = job["id"]
        return len(jobs)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # A task cancelled before its first instruction cannot run its handler.
        with s.connection() as db:
            for job_id in self.tasks.values():
                db.execute(
                    "UPDATE jobs SET status='queued',started_at=NULL WHERE id=? AND status='running'",
                    (job_id,),
                )
        self.tasks.clear()
        self.active_wave = None


async def housekeeping():
    next_maintenance = {}
    while True:
        for user_id in accounts.active_tenants():
            with s.tenant_scope(user_id):
                try:
                    await process_deliveries()
                    await prediction.schedule()
                    if time.monotonic() >= next_maintenance.get(user_id, 0):
                        maintenance()
                        next_maintenance[user_id] = time.monotonic() + 60
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    s.log(f"后台维护异常（{type(exc).__name__}），将在下轮恢复", level="error")
        await asyncio.sleep(2)


def recover_jobs():
    prediction.recover()
    with s.connection() as db:
        db.execute("UPDATE jobs SET status='success',finished_at=? WHERE status='running' AND id IN (SELECT job_id FROM observations WHERE status='success')", (s.now(),))
        db.execute("UPDATE jobs SET status='failed',finished_at=? WHERE status='running' AND id IN (SELECT job_id FROM observations)", (s.now(),))
        db.execute("UPDATE jobs SET status='queued',started_at=NULL WHERE status='running'")


async def run():
    pools = {}
    services = None
    turn = 0
    try:
        while True:
            active = accounts.active_tenants()
            for user_id in active:
                if user_id not in pools:
                    with s.tenant_scope(user_id):
                        s.init()
                        recover_jobs()
                        pools[user_id] = CollectionPool()
            for user_id in list(pools):
                if user_id not in active:
                    with s.tenant_scope(user_id):
                        await pools.pop(user_id).close()
            if services is None:
                services = asyncio.create_task(housekeeping())
                training_queue.kick()
            # Rotate dispatch order so waiting accounts get a turn at free slots.
            ordered = active[turn % len(active):] + active[:turn % len(active)] if active else []
            turn += 1
            for user_id in ordered:
                with s.tenant_scope(user_id):
                    try:
                        schedule()
                        capacity = None
                        if user_id != "admin":
                            occupied = sum(
                                not task.done()
                                for owner, pool in pools.items() if owner != "admin"
                                for task in pool.tasks
                            )
                            capacity = MAX_ORDINARY_CONCURRENCY - occupied
                        # No await between counting and dispatch: new tasks
                        # reserve their slots before another account is checked.
                        pools[user_id].dispatch(capacity=capacity)
                        collection_queue.publish(pools)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        s.log(f"后台调度异常（{type(exc).__name__}），将在下轮恢复", level="error")
            await asyncio.sleep(POLL_SECONDS)
    finally:
        if services:
            services.cancel()
            await asyncio.gather(services, return_exceptions=True)
        for user_id, pool in pools.items():
            with s.tenant_scope(user_id):
                await pool.close()
        collection_queue.clear()
