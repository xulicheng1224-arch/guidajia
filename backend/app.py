import asyncio
import csv
import fcntl
import io
import json
import os
import secrets
import sqlite3
import tempfile
import zipfile
from contextlib import asynccontextmanager, closing, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.background import BackgroundTask
from diagnostics import describe

from . import access, accounts, collector, domain as d, prediction, store as s, worker, training_queue, collection_queue, session_verification, full_backup, price_sources, product_urls, public_view
from . import (
    collection_batches,
    collection_frequency,
    collection_results,
    comparison,
    csv_io,
    prediction_data as pd,
)
from .models import MODELS, product_key
from . import outbound
from .request_limits import RequestLimitsMiddleware

TOKEN_FILE = s.DATA / "access-token"


@asynccontextmanager
async def lifespan(app):
    task, lock = None, None
    if os.environ.get("MONITOR_DISABLE_WORKER") != "1":
        lock = (s.DATA / "service.lock").open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("已有监测服务或恢复操作在运行，请勿重复启动")
    accounts.init()
    if os.environ.get("MONITOR_DISABLE_WORKER") != "1" and not any(u["role"] == "admin" for u in accounts.users()):
        raise RuntimeError("请先运行 tools/manage_accounts.py create-admin 创建唯一管理员")
    s.init()
    previous_session = s.ROOT / "session.json"
    if previous_session.is_file() and collector.session_state.can_migrate(collector.runtime().SESSION) and not (s.DATA / "restored-state.json").exists():
        try:
            state = json.loads(previous_session.read_text())
            if isinstance(state.get("cookies"), list):
                collector.session_state.save(collector.runtime().SESSION, state)
        except (OSError, ValueError, TypeError):
            s.log("旧会话文件无法导入，请在设置中重新登录", level="warning")
    if os.environ.get("MONITOR_DISABLE_WORKER") != "1":
        training_queue.recover()
        task = asyncio.create_task(worker.run())
    yield
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    await training_queue.shutdown()
    for user_id in [u["id"] for u in accounts.users()]:
        with s.tenant_scope(user_id):
            if collector.runtime().login_task and not collector.runtime().login_task.done():
                collector.runtime().login_task.cancel()
                with suppress(asyncio.CancelledError):
                    await collector.runtime().login_task
            if prediction.runtime().TRAINING and not prediction.runtime().TRAINING.done():
                prediction.runtime().TRAINING.cancel()
                with suppress(asyncio.CancelledError):
                    await prediction.runtime().TRAINING
    if lock:
        lock.close()


app = FastAPI(
    title="鬼打架 · 硬件价格监测",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
)


@app.middleware("http")
async def check_access(request: Request, call_next):
    response = access.guard(request)
    if response is None:
        try:
            user = getattr(request.state, "user", None)
            with s.tenant_scope(user["id"] if user else "admin"):
                response = await call_next(request)
        except ValueError as exc:
            response = JSONResponse({"detail": public_view.text(str(exc))}, status_code=409)
        except KeyError:
            response = JSONResponse({"detail": "记录不存在"}, status_code=404)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if (response.headers.get("content-type", "").startswith("text/html")
            and "Content-Security-Policy" not in response.headers
            and request.url.path not in ("/api/docs", "/redoc", "/docs/oauth2-redirect")):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
    for header in ("server", "x-powered-by", "x-runtime", "x-aspnet-version"):
        if header in response.headers:
            del response.headers[header]
    if request.url.path.startswith("/api/") or getattr(request.state, "remote", False) or not getattr(request.state, "user", None):
        response.headers["Cache-Control"] = "no-store"
    if getattr(request.state, "remote", False):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


app.add_middleware(RequestLimitsMiddleware)


@app.exception_handler(HTTPException)
async def public_http_error(request, exc):
    return JSONResponse({"detail": public_view.technical(exc.detail)}, status_code=exc.status_code, headers=exc.headers)


@app.api_route("/brand/guidajia.jpg", methods=["GET", "HEAD"], include_in_schema=False)
def brand_image():
    return FileResponse(
        s.ROOT / "public" / "brand" / "guidajia.jpg", media_type="image/jpeg"
    )


@app.get("/api/ping")
def ping():
    return {"ok": True}


@app.get("/api/bootstrap")
def bootstrap(request: Request):
    return {"ok": True, "remote": request.state.remote, "user": request.state.user}


def set_account_cookie(response, request, token, duration):
    response.set_cookie(accounts.COOKIE, token, max_age=duration, secure=request.state.remote,
                        httponly=True, samesite="strict", path="/")
    # Retire both previous authentication formats on this browser.
    response.delete_cookie("monitor_session", path="/")
    response.delete_cookie(access.REMOTE_COOKIE, path="/")


async def account_body(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            raise HTTPException(413, "账户请求过大")
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "账户请求格式无效") from None


def account_peer(request):
    return request.headers.get("cf-connecting-ip", request.client.host if request.client else "unknown")[:100]


@app.get("/api/access/options")
def account_options():
    return {"registration": True, "email_verification": False, "password_reset": "admin"}


@app.post("/api/access/login")
async def access_login(request: Request):
    body = await account_body(request)
    accounts.limit("login-peer:" + account_peer(request), 20, 300)
    email = accounts.email_address(body.get("email"))
    accounts.limit("login-email:" + email, 10, 300)
    user, version = await asyncio.to_thread(accounts.authenticate, email, body.get("password"))
    token, duration = accounts.create_session(user["id"], body.get("remember") is True, version)
    accounts.clear_login_failures(email)
    response = JSONResponse({"ok": True, "role": user["role"], "user": user})
    set_account_cookie(response, request, token, duration)
    return response


@app.post("/api/access/register")
async def account_register(request: Request):
    body = await account_body(request)
    accounts.limit("register-peer:" + account_peer(request), 10, 3600)
    user_id = await asyncio.to_thread(accounts.register, body.get("email"), body.get("password"))
    token, duration = accounts.create_session(user_id)
    response = JSONResponse({"ok": True, "role": "user"})
    set_account_cookie(response, request, token, duration)
    return response


@app.post("/api/access/logout")
def access_logout(request: Request):
    accounts.logout(request.cookies.get(accounts.COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(accounts.COOKIE, path="/", secure=request.state.remote, httponly=True, samesite="strict")
    return response


@app.post("/api/account/password")
async def account_password(request: Request):
    body = await account_body(request)
    accounts.limit("change-password:" + request.state.user["id"], 10, 300)
    await asyncio.to_thread(accounts.change_password, request.state.user["id"], body.get("current_password"), body.get("password"))
    return {"ok": True}


@app.get("/api/security/users")
def account_users():
    return accounts.users()


@app.patch("/api/security/users/{user_id}")
def account_status(user_id: str, body: dict):
    accounts.set_disabled(user_id, body.get("disabled"))
    return {"ok": True}


@app.post("/api/security/users/{user_id}/password")
async def account_user_password(user_id: str, request: Request):
    body = await account_body(request)
    accounts.limit("admin-password:" + account_peer(request), 20, 300)
    await asyncio.to_thread(accounts.admin_reset_password, user_id, body.get("password"))
    return {"ok": True}


def required(entity_id, kind=None, deleted=False):
    result = s.get(entity_id, kind, include_deleted=deleted)
    if not result:
        raise HTTPException(404, "记录不存在")
    return result


def public_settings():
    values = s.settings()
    values["price_apis"] = price_sources.public(values)["providers"]
    values["channels"] = {
        key: {
            **{k: v for k, v in config.items() if k not in ("url", "password")},
            "configured": bool(config.get("url") or config.get("password")),
            "url": "__saved__" if config.get("url") else "",
            "password": "__saved__" if config.get("password") else "",
        }
        for key, config in values.get("channels", {}).items()
    }
    if s.TENANT.get() != "admin":
        values.pop("browser", None)
        values.pop("crawler_headed", None)
    return values


@app.get("/api/state")
def state(request: Request):
    return {
        "access": access.status(request),
        "products": public_view.business(d.products()),
        "health": public_view.technical(d.health()),
        "settings": public_settings(),
        "session": collector.session_info(),
        "events": s.entities("event"),
        "rules": s.entities("rule"),
        "views": s.entities("view"),
    }


@app.get("/api/product/{product_id}")
def product_detail(product_id: str):
    required(product_id, "product")
    p = next(p for p in d.products() if p["id"] == product_id)
    rows = d.records(product_id)
    with s.connection() as db:
        forecasts = [
            {**dict(r), "data": json.loads(r["data"])}
            for r in db.execute(
                "SELECT * FROM forecasts WHERE product_id=? ORDER BY created_at DESC LIMIT 200",
                (product_id,),
            )
        ]
    from . import prediction_data as pd

    history_series = pd.daily_series(
        rows, {**s.settings(), "include_coupon": True}, d.utcnow()
    )
    for f in forecasts:
        origin = pd.day(f["created_at"])
        ends = pd.end_of_day(origin + timedelta(days=f["data"]["window"]))
        series_key = f["data"].get("series_key")
        outcome = (
            pd.future_outcome(
                history_series.get((product_id, series_key), []),
                origin,
                f["data"]["window"],
                d.utcnow(),
            )
            if series_key
            else None
        )
        f["actual_min"] = outcome["minimum"] if outcome else None
        f["matured"] = ends <= d.utcnow()
        f["evaluation_status"] = (
            "legacy"
            if not series_key
            else (
                "pending"
                if not f["matured"]
                else "evaluated" if outcome else "insufficient"
            )
        )
    return {
        "product": public_view.business(p),
        "records": public_view.technical(rows),
        "changes": d.changes(rows, s.settings()["confirm_count"]),
        "notes": [r for r in s.entities("note") if r["product_id"] == product_id],
        "forecasts": forecasts,
    }


@app.get("/api/entities/{kind}")
def list_entities(kind: str):
    if kind not in MODELS:
        raise HTTPException(404, "未知对象类型")
    return s.entities(kind)


def validated(kind, data, old=None):
    if kind not in MODELS:
        raise HTTPException(404, "未知对象类型")
    try:
        result = (
            MODELS[kind]
            .model_validate({**(s.payload(old) if old else {}), **data})
            .model_dump(mode="json")
        )
    except ValidationError as exc:
        raise HTTPException(422, "；".join(e["msg"] for e in exc.errors()))
    if kind == "product":
        if not result["name"]:
            result["name"] = (
                "待识别商品 · " + product_urls.identity(result["url"])["item_id"]
            )
        duplicates = [
            p
            for p in s.entities("product")
            if product_key(p) == product_key(result)
            and (not old or p["id"] != old["id"])
        ]
        if duplicates:
            raise HTTPException(409, "该商品与 SKU 已在商品库中")
    if kind == "note":
        required(result["product_id"], "product")
    if kind == "event":
        result["known_at"] = old.get("known_at", old["created_at"]) if old else s.now()
    return result


@app.post("/api/entities/{kind}")
def create_entity(kind: str, body: dict):
    data = validated(kind, body)
    item = s.create(kind, data)
    if kind == "product" and not data["paused"] and price_sources.supported(data):
        s.enqueue(item["id"])
        s.update(
            item["id"],
            {
                "last_scheduled": s.now(),
                "next_run": (
                    d.utcnow() + timedelta(hours=item["interval_hours"])
                ).isoformat(),
            },
            audited=False,
        )
    return item


@app.put("/api/entities/{kind}/{entity_id}")
def update_entity(kind: str, entity_id: str, body: dict):
    old = required(entity_id, kind)
    if (
        kind == "product"
        and body.get("paused") is False
        and not body.get("url", old.get("url"))
    ):
        raise HTTPException(422, "请先补充淘宝商品链接，再恢复采集")
    data = validated(kind, body, old)
    return s.update(
        entity_id, data, version=body.get("version"), reason=body.get("reason", "")
    )


@app.delete("/api/entities/{kind}/{entity_id}")
def delete_entity(kind: str, entity_id: str):
    required(entity_id, kind)
    if kind not in MODELS:
        raise HTTPException(400, "不能删除此类型")
    result = s.update(
        entity_id, {"paused": True} if kind == "product" else {}, deleted_at=s.now()
    )
    if kind == "view" and s.settings()["default_view"] == entity_id:
        s.set_settings({"default_view": None})
    return result


@app.post("/api/products/bulk")
def bulk(body: dict):
    ids = list(dict.fromkeys(body.get("ids", [])))
    if not ids or len(ids) > 500:
        raise HTTPException(422, "请选择 1–500 个商品")
    action = body.get("action")
    if action not in (
        "pause",
        "resume",
        "group",
        "tags",
        "interval",
        "delete",
        "crawl",
    ):
        raise HTTPException(422, "不支持的批量操作")
    selected = [required(i, "product") for i in ids]
    if action in ("resume", "crawl") and any(not p.get("url") for p in selected):
        raise HTTPException(422, "所选商品中有待补链接的参考商品，请先补充淘宝链接")
    prepared = []
    for p in selected:
        changes = {}
        if action in ("pause", "resume"):
            changes = {"paused": action == "pause"}
        if action == "group":
            changes = {"group": body.get("value", "")}
        if action == "tags":
            changes = {"tags": list(dict.fromkeys(p["tags"] + body.get("value", [])))}
        if action == "interval":
            changes = {"interval_hours": body.get("value")}
        if action == "delete":
            changes = {"paused": True}
        prepared.append((p, validated("product", changes, p)))
    with s.connection() as db:
        for p, data in prepared:
            if action == "crawl":
                continue
            after = {
                **p,
                **data,
                "version": p["version"] + 1,
                "updated_at": s.now(),
                "deleted_at": s.now() if action == "delete" else None,
            }
            db.execute(
                "UPDATE entities SET data=?,version=?,updated_at=?,deleted_at=? WHERE id=?",
                (
                    s.dumps(s.payload(after)),
                    after["version"],
                    after["updated_at"],
                    after["deleted_at"],
                    p["id"],
                ),
            )
            s.audit(db, p["id"], "product", "bulk", p, after, "批量" + action)
    if action == "crawl":
        for p in selected:
            s.enqueue(p["id"], manual=True)
    return {"count": len(ids)}


@app.post("/api/product/{product_id}/crawl")
def crawl_product(product_id: str):
    product = required(product_id, "product")
    if not product.get("url"):
        raise HTTPException(409, "请先补充商品链接")
    return {"job_id": s.enqueue(product_id, manual=True), "price": None, "message": "已加入手动采集队列"}


@app.post("/api/product/{product_id}/override")
def override(product_id: str, body: dict):
    required(product_id, "product")
    label = body.get("label")
    if label and label not in (
        "建议观望",
        "接近阶段低点",
        "可能涨价",
        "平稳",
        "数据不足",
    ):
        raise HTTPException(422, "无效标签")
    until = body.get("until")
    if label and (not until or d.dt(until) <= d.utcnow()):
        raise HTTPException(422, "请选择未来的到期时间")
    return s.update(
        product_id,
        {"override_label": label, "override_until": until if label else None},
        reason=body.get("reason", "人工覆盖展示标签"),
    )


@app.get("/api/product/{product_id}/comparison-days")
def comparison_days(product_id: str, raw: bool = False):
    return comparison.daily(required(product_id, "product"), raw)


@app.get("/api/product/{product_id}/daily-prices")
def daily_price_list(product_id: str, basis: str = "regular"):
    from . import daily_prices

    product = required(product_id, "product")
    return daily_prices.report(product, d.records(product_id), basis)


@app.post("/api/product/{product_id}/daily-prices")
def daily_price_save(product_id: str, body: dict):
    from . import daily_prices

    product = required(product_id, "product")
    return daily_prices.save(product, d.records(product_id), body)


@app.post("/api/daily-prices/{override_id}/undo")
def daily_price_undo(override_id: str, body: dict):
    override = required(override_id, "daily_price")
    required(override["product_id"], "product")
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HTTPException(422, "无效版本号，请刷新后重试")
    return s.update(
        override_id, {}, version=version, deleted_at=s.now(), reason="撤销按天编辑价格"
    )


@app.post("/api/product/{product_id}/records")
def manual_record(product_id: str, body: dict):
    required(product_id, "product")
    price = collector.cents(body.get("price"))
    if price is None:
        raise HTTPException(422, "请输入大于 0 的价格")
    stamp = body.get("observed_at") or s.now()
    if d.dt(stamp) > d.utcnow() + timedelta(minutes=1):
        raise HTTPException(422, "价格时间不能在未来")
    stamp = d.dt(stamp).isoformat()
    stock = body.get("stock", "in_stock")
    if stock not in ("in_stock", "out_of_stock", "unknown", "low_stock", "delisted"):
        raise HTTPException(422, "无效库存状态")
    result = {
        "price": price,
        "original_price": collector.cents(body.get("original_price")),
        "stock": stock,
        "status": "success",
        "price_type": body.get("price_type", "regular"),
        "note": str(body.get("reason", ""))[:5000],
    }
    return {
        "id": d.save_observation(product_id, result, source="manual", observed_at=stamp)
    }


@app.post("/api/records/{record_id}/correct")
def correct(record_id: str, body: dict):
    with s.connection() as db:
        raw = db.execute(
            "SELECT * FROM observations WHERE id=?", (record_id,)
        ).fetchone()
    if not raw:
        raise HTTPException(404, "价格记录不存在")
    required(raw["product_id"], "product")
    correction_id = "correction_" + record_id
    old = s.get(correction_id, "correction", include_deleted=True)
    data = {
        "observation_id": record_id,
        "product_id": raw["product_id"],
        "reason": str(body.get("reason", ""))[:5000],
    }
    if "price" in body:
        data["price"] = collector.cents(body["price"])
        if data["price"] is None:
            raise HTTPException(422, "请输入有效价格")
    for key in ("valid", "exclude_prediction"):
        if key in body:
            if not isinstance(body[key], bool):
                raise HTTPException(422, "无效布尔值")
            data[key] = body[key]
    if "stock" in body:
        if body["stock"] not in (
            "in_stock",
            "out_of_stock",
            "low_stock",
            "unknown",
            "delisted",
        ):
            raise HTTPException(422, "无效库存")
        data["stock"] = body["stock"]
    if "original_price" in body:
        data["original_price"] = collector.cents(body["original_price"])
    if old:
        return s.update(
            correction_id,
            data,
            version=body.get("version"),
            reason=data["reason"],
            deleted_at=None,
        )
    return s.create("correction", data, entity_id=correction_id, reason=data["reason"])


@app.post("/api/records/{record_id}/undo")
def undo_record(record_id: str):
    required("correction_" + record_id, "correction")
    return s.update(
        "correction_" + record_id, {}, deleted_at=s.now(), reason="撤销人工覆盖"
    )


@app.post("/api/records/bulk")
def bulk_records(body: dict):
    ids = body.get("ids", [])
    if not ids or len(ids) > 1000:
        raise HTTPException(422, "请选择 1–1000 条记录")
    with s.connection() as db:
        found = {
            r["id"]
            for r in db.execute(
                "SELECT id FROM observations WHERE id IN ("
                + ",".join("?" for _ in ids)
                + ")",
                ids,
            )
        }
    if found != set(ids):
        raise HTTPException(404, "部分记录已不存在")
    for record_id in ids:
        correct(
            record_id,
            {
                "valid": bool(body.get("valid", False)),
                "reason": body.get("reason", "批量标记"),
            },
        )
    return {"count": len(ids)}


@app.post("/api/changes/{change_id}")
def change_flag(change_id: str, body: dict):
    if not change_id.startswith("change_"):
        raise HTTPException(422, "无效变价事件")
    data = {
        "excluded": bool(body.get("excluded", False)),
        "mark": str(body.get("mark", ""))[:200],
    }
    old = s.get(change_id, "change")
    return (
        s.update(change_id, data)
        if old
        else s.create("change", data, entity_id=change_id)
    )


@app.get("/api/logs")
def logs(product_id: str | None = None):
    with s.connection() as db:
        rows = db.execute(
            "SELECT * FROM logs"
            + (" WHERE product_id=?" if product_id else "")
            + " ORDER BY created_at DESC LIMIT 1000",
            (product_id,) if product_id else (),
        ).fetchall()
    return [
        {**dict(r), "message": public_view.text(r["message"]),
         "details": public_view.technical(json.loads(r["details"])) if r["details"] else None}
        for r in rows
    ]


@app.get("/api/jobs")
def jobs():
    with s.connection() as db:
        rows = db.execute(
            "SELECT jobs.*, observations.data AS observation_data, observations.status AS observation_status FROM jobs LEFT JOIN observations ON observations.job_id=jobs.id ORDER BY jobs.created_at DESC LIMIT 200"
        ).fetchall()
    output = []
    queue_context = collection_queue.snapshot() if any(row["status"] == "queued" for row in rows) else None
    for row in rows:
        item = dict(row)
        context = json.loads(item.get("context") or "{}")
        item["purpose"] = context.get("purpose")
        if item["status"] == "running" and context.get("pacing"):
            item["pacing"] = context["pacing"]
        observation_data = item.pop("observation_data")
        observation_status = item.pop("observation_status")
        result = json.loads(item["result"] or "{}")
        observation = json.loads(observation_data or "{}")
        item["diagnostics"] = result.get("diagnostics") or (
            describe({**observation, "status": observation_status or item["error"]})
            if item["error"]
            else None
        )
        if item["status"] == "queued":
            item["queue"] = collection_queue.explain(item, queue_context)
        output.append(public_view.technical(item))
    return output


@app.post("/api/jobs/{job_id}/cancel")
def cancel_queued_job(job_id: str):
    with s.connection() as db:
        row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, "任务不存在")
        if row[0] == "cancelled":
            return {"ok": True}
        if row[0] != "queued":
            raise HTTPException(409, "只能取消尚未开始的任务")
        db.execute("UPDATE jobs SET status='cancelled',finished_at=?,context=json_set(context,'$.released_at',?) WHERE id=? AND status='queued'", (s.now(), s.now(), job_id))
    return {"ok": True}


@app.get("/api/collection/results")
def completed_collection_results(
    after: str | None = None, limit: int = Query(100, ge=1, le=100)
):
    try:
        return public_view.technical(collection_results.feed(after, limit))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.post("/api/collection/frequency")
def collection_frequency_save(body: dict):
    return collection_frequency.apply(body.get("interval_hours"))


@app.post("/api/collection/{action}")
def collection_action(action: str):
    if action == "all":
        return {
            **collection_batches.enqueue_all(),
            "health": public_view.technical(d.health()),
            "session": collector.session_info(),
        }
    elif action in ("pause", "resume"):
        s.set_settings({"global_paused": action == "pause"})
    elif action == "retry":
        for p in d.products():
            if p.get("url") and p["failures"]:
                s.enqueue(p["id"], manual=True)
    elif action == "circuit":
        s.set_settings(
            {
                "circuit_until": (
                    d.utcnow() + timedelta(hours=s.settings()["circuit_hours"])
                ).isoformat()
            }
        )
    elif action == "uncircuit":
        s.set_settings({"circuit_until": None, "failures": 0})
        s.log("已手动解除熔断，连续失败计数已清零")
    elif action == "probe":
        p = next((p for p in s.entities("product") if p.get("url")), None)
        if p:
            s.enqueue(p["id"], manual=True)
    else:
        raise HTTPException(404, "未知操作")
    return public_view.technical(d.health())


@app.post("/api/session/import")
async def session_import(body: dict):
    content = body.get("content") if isinstance(body, dict) else None
    try:
        state, rejected = collector.session_state.normalize_uploaded(content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    coverage = collector.session_state.import_diagnostics(content, state)
    export_note = f"；导出工具 v{coverage['source_version']}" if coverage["source_version"] else ""
    if coverage["reported_storage_read_failures"]:
        export_note += f"，文件报告 {coverage['reported_storage_read_failures']} 处页面存储读取失败"
    if coverage["cookie_count_mismatch"]:
        export_note += "，文件声明的 Cookie 数量与实际导入数量不一致"
    partitioned = sum(bool(c.get("partitionKey")) for c in state["cookies"])
    origins = len(state.get("origins", []))
    await collector.import_session(
        state,
        f"已导入 {len(state['cookies'])} 条 Cookie（含 {partitioned} 条分区记录）、{origins} 个本地存储来源{export_note}；登录有效性待实际采集验证",
    )
    s.log(
        f"已导入淘宝 Cookie：{len(state['cookies'])} 条"
        + f"，分区 Cookie {partitioned} 条，本地存储 {origins} 个来源"
        + (f"，忽略 {rejected} 条不支持记录" if rejected else "")
        + export_note,
        level="warning" if coverage["reported_storage_read_failures"] or coverage["cookie_count_mismatch"] else "info",
        details={"session_import": coverage},
    )
    return collector.session_info()


@app.post("/api/session/verify")
def verify_session(body: dict):
    if price_sources.mode() != "crawler":
        raise HTTPException(409, "当前启用官方 API 分支，切回爬虫后才能校验 Cookie")
    if collector.runtime().LOGIN["status"] in ("opening", "waiting"):
        raise HTTPException(409, "请先结束当前淘宝登录操作")
    if s.TENANT.get() != "admin":
        return session_verification.enqueue_random(collector.runtime().SESSION)
    product = required(body.get("product_id", ""), "product")
    if not product.get("url"):
        raise HTTPException(422, "请选择已填写淘宝商品链接的商品")
    collector.normalize_url(product["url"])
    if product_urls.identity(product["url"])["platform"] != "taobao":
        raise HTTPException(422, "Cookie 校验仅适用于淘宝或天猫商品")
    return session_verification.enqueue(product, collector.runtime().SESSION)


@app.get("/api/session/export-script/{platform}")
def session_export_script(platform: str):
    # Keep old download links compatible; all platforms get the same complete
    # browser-native extension rather than scripts requiring Python packages.
    if platform not in ("mac", "windows", "extension"):
        raise HTTPException(404, "不支持的脚本平台")
    archive = io.BytesIO()
    extension = s.ROOT / "tools" / "cookie-extension"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        # Fixed allowlist: never include credentials, user files or build cache.
        for name in ("manifest.json", "popup.html", "popup.css", "popup.js", "cookies.mjs", "使用说明.txt"):
            bundle.write(extension / name, name)
    archive.seek(0)
    filename = "taobao-cookie-export-extension.zip"
    return Response(
        archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/session/{action}")
async def session_action(action: str):
    if action == "login":
        if collector.runtime().login_task and not collector.runtime().login_task.done():
            return collector.session_info()
        collector.runtime().login_task = asyncio.create_task(collector.login())
        await asyncio.sleep(0)
    elif action == "complete":
        if collector.runtime().LOGIN["status"] != "waiting":
            raise HTTPException(409, "没有等待完成的登录窗口")
        collector.runtime().login_done.set()
    elif action == "retry":
        if (
            collector.runtime().LOGIN["status"] != "waiting"
            or not collector.runtime().login_task
            or collector.runtime().login_task.done()
        ):
            raise HTTPException(409, "没有等待重试的登录窗口")
        collector.runtime().login_retry.set()
    elif action == "cancel":
        await collector.cancel_login()
    elif action == "clear":
        async with collector.runtime().SESSION_LOCK:
            await collector.cancel_login()
            collector.session_state.clear(collector.runtime().SESSION)
            session_verification.invalidate(collector.runtime().SESSION)
            collector.runtime().LOGIN.update(status="idle", message="登录状态已清除", issue=None)
    else:
        raise HTTPException(404, "未知操作")
    return collector.session_info()


@app.get("/api/deliveries")
def deliveries():
    with s.connection() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM deliveries ORDER BY created_at DESC LIMIT 1000"
            )
        ]


@app.post("/api/deliveries/{delivery_id}/retry")
def retry_delivery(delivery_id: str):
    with s.connection() as db:
        if not db.execute(
            "SELECT 1 FROM deliveries WHERE id=?", (delivery_id,)
        ).fetchone():
            raise HTTPException(404, "记录不存在")
        db.execute(
            "UPDATE deliveries SET status='pending',attempts=0,next_attempt=NULL,error=NULL WHERE id=?",
            (delivery_id,),
        )
    return {"ok": True}


@app.post("/api/channels/{channel}/test")
async def test_channel(channel: str):
    accounts.limit("notification-test:" + s.TENANT.get(), 10, 60)
    try:
        await worker.send(channel, "鬼打架测试通知 · 通知渠道连接成功", s.settings())
    except Exception as exc:
        raise HTTPException(
            400,
            (
                str(exc)
                if isinstance(exc, ValueError)
                else "发送失败，请检查渠道配置和网络"
            ),
        )
    return {"ok": True}


@app.post("/api/rules/preview")
def preview_rule(body: dict):
    rule = validated("rule", body)
    count, products = 0, set()
    for p in s.entities("product"):
        rows = d.records(p["id"])
        active, last_sent = False, None
        for i, row in enumerate(rows):
            if d.dt(row["observed_at"]) < d.utcnow() - timedelta(days=90):
                continue
            hit, _ = d.rule_matches(rule, rows[: i + 1], p, d.dt(row["observed_at"]))
            if (
                hit
                and not active
                and (
                    not last_sent
                    or d.dt(row["observed_at"]) - last_sent
                    >= timedelta(minutes=rule["cooldown_minutes"])
                )
            ):
                count += 1
                products.add(p["id"])
                last_sent = d.dt(row["observed_at"])
            active = hit
    return {"triggers": count, "products": len(products), "days": 90}


@app.post("/api/events/generate")
def generate_events(body: dict):
    year = int(body.get("year", d.utcnow().year))
    if not 2020 <= year <= 2100:
        raise HTTPException(422, "年份应在 2020–2100 之间")
    existing = {e["name"] for e in s.entities("event")}
    count = 0
    for suffix, month, day in [
        ("618 固定日期参考", 6, 18),
        ("双 11 固定日期参考", 11, 11),
        ("双 12 固定日期参考", 12, 12),
    ]:
        name = f"{year} {suffix}"
        if name in existing:
            continue
        date = f"{year}-{month:02d}-{day:02d}"
        s.create(
            "event",
            validated(
                "event",
                {
                    "name": name,
                    "start": date,
                    "end": date,
                    "confidence": "待确认",
                    "notes": "固定日期参考，实际活动窗口请按平台公告确认",
                },
            ),
        )
        count += 1
    return {"count": count}


@app.post("/api/forecasts/recalculate")
def recalculate(body: dict):
    config = s.settings()
    config.update(body.get("thresholds", {}))
    # Validate proposed settings before using them for a preview.
    validate_settings({k: v for k, v in config.items() if k in s.DEFAULTS})
    output = []
    prediction_time = d.utcnow()
    for p in s.entities("product"):
        prediction = d.forecast(p, d.records(p["id"]), config, as_of=prediction_time)
        output.append({"product_id": p["id"], "name": p["name"], **prediction})
    if not body.get("preview"):
        with s.connection() as db:
            for item in output:
                db.execute(
                    "INSERT INTO forecasts VALUES(?,?,?,?)",
                    (s.uid(), item["product_id"], s.now(), s.dumps(item)),
                )
    return output


@app.get("/api/forecast-model")
def forecast_model_status():
    return public_view.technical(prediction.report())


@app.post("/api/forecast-model/train")
async def train_forecast_model():
    scheduled = await prediction.schedule(force=True)
    return {
        "scheduled": scheduled,
        "message": "训练请求已加入全服务器单任务队列" if scheduled else "当前账号已有训练请求，未重复提交",
        "queue": training_queue.status(),
    }


def validate_settings(values):
    ranges = {
        "wait_seconds": (0, 120),
        "request_gap": (1, 300),
        "crawler_start_gap": (1, 120),
        "max_retries": (0, 3),
        "max_concurrency": (1, 5),
        "circuit_failures": (1, 100),
        "circuit_hours": (0.01, 72),
        "confirm_count": (1, 10),
        "anomaly_percent": (1, 1000),
        "delist_count": (2, 20),
        "delist_hours": (1, 168),
        "log_days": (1, 3650),
        "trash_days": (1, 3650),
        "observation_years": (1, 20),
        "backup_hour": (0, 23),
        "backup_keep": (1, 100),
        "event_boost_days": (0, 30),
        "event_interval_minutes": (6, 1440),
        "default_range": (1, 3650),
        "forecast_days": (1, 365),
        "min_days": (7, 365),
        "min_changes": (1, 100),
        "low_percentile": (0, 100),
        "drop_threshold": (0, 100),
        "digest_minutes": (0, 1440),
    }
    for key, value in values.items():
        if key not in s.DEFAULTS:
            raise HTTPException(422, f"未知设置：{key}")
        if key in ranges:
            low, high = ranges[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not low <= value <= high
            ):
                raise HTTPException(422, f"{key} 应在 {low}–{high} 之间")
            if isinstance(s.DEFAULTS[key], int) and not isinstance(value, int):
                raise HTTPException(422, f"{key} 必须为整数")
        elif isinstance(s.DEFAULTS[key], bool) and not isinstance(value, bool):
            raise HTTPException(422, f"{key} 必须为布尔值")
    if values.get("browser", s.settings()["browser"]) not in (
        "msedge",
        "chrome",
        "chromium",
    ):
        raise HTTPException(422, "请选择支持的浏览器")
    if values.get("theme", "light") not in ("light", "dark", "system"):
        raise HTTPException(422, "无效主题")
    import re

    for key in ("quiet_start", "quiet_end"):
        if (
            key in values
            and values[key]
            and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", values[key])
        ):
            raise HTTPException(422, "免打扰时间应为 HH:MM")
    if "channels" in values and not isinstance(values["channels"], dict):
        raise HTTPException(422, "通知渠道格式错误")
    if "dashboard_cards" in values or "card_sizes" in values:
        from .models import View

        try:
            View(
                name="settings",
                config={
                    k: values[k]
                    for k in ("dashboard_cards", "card_sizes")
                    if k in values
                },
            )
        except ValidationError:
            raise HTTPException(422, "看板卡片或宽度配置无效")
    if "chart_layers" in values and (
        not isinstance(values["chart_layers"], list)
        or any(
            v not in ("sale", "original", "events", "notes", "manual")
            for v in values["chart_layers"]
        )
    ):
        raise HTTPException(422, "图表图层配置无效")
    if values.get("density", "middle") not in ("small", "middle", "large"):
        raise HTTPException(422, "无效表格密度")


@app.get("/api/price-source")
def price_source_get():
    return price_sources.public()


@app.put("/api/price-source")
def price_source_save(body: dict):
    return price_sources.save(body)


@app.patch("/api/settings")
def update_settings(body: dict):
    if s.TENANT.get() != "admin" and {"browser", "crawler_headed"} & body.keys():
        raise HTTPException(403, "只有管理员可以修改浏览器运行配置")
    protected = {"circuit_until", "failures", "last_backup", "price_source", "price_source_revision", "price_apis"}
    if protected & body.keys():
        raise HTTPException(422, "运行状态不可直接修改")
    validate_settings(body)
    if "channels" in body:
        channels = s.settings()["channels"]
        for key, config in body["channels"].items():
            if key not in (
                "bark",
                "serverchan",
                "wecom",
                "feishu",
                "email",
            ) or not isinstance(config, dict):
                raise HTTPException(422, "不支持的通知渠道")
            clean = {
                k: v
                for k, v in config.items()
                if k in ("enabled", "url", "host", "port", "user", "to", "password")
                and v != "__saved__"
            }
            channels[key] = {**channels.get(key, {}), **clean}
            try:
                outbound.validate_config(key, channels[key])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        body["channels"] = channels
    s.set_settings(body)
    s.log("系统设置已更新")
    return public_settings()


@app.get("/api/audit")
def audit_list():
    with s.connection() as db:
        return [
            {
                **dict(r),
                "before_data": (
                    json.loads(r["before_data"]) if r["before_data"] else None
                ),
                "after_data": json.loads(r["after_data"]) if r["after_data"] else None,
            }
            for r in db.execute(
                "SELECT * FROM audit ORDER BY created_at DESC LIMIT 2000"
            )
        ]


@app.post("/api/audit/{audit_id}/rollback")
def rollback(audit_id: str, body: dict):
    with s.connection() as db:
        record = db.execute("SELECT * FROM audit WHERE id=?", (audit_id,)).fetchone()
        if not record:
            raise HTTPException(404, "审计记录不存在")
        if record["reverted_at"]:
            raise HTTPException(409, "此操作已经回滚")
        before = json.loads(record["before_data"]) if record["before_data"] else None
        after = json.loads(record["after_data"]) if record["after_data"] else None
        if record["kind"] == "manual":
            # Apply the covering record within this transaction (no nested writers).
            original = db.execute(
                "SELECT * FROM observations WHERE id=?", (record["entity_id"],)
            ).fetchone()
            if not original:
                raise HTTPException(409, "原始记录已不存在")
            cid = "correction_" + record["entity_id"]
            old = s.decode(
                db.execute("SELECT * FROM entities WHERE id=?", (cid,)).fetchone()
            )
            stamp = s.now()
            values = {
                **(s.payload(old) if old else {}),
                "product_id": original["product_id"],
                "observation_id": original["id"],
                "valid": False,
                "reason": "回滚手动新增价格",
            }
            db.execute(
                "INSERT INTO entities VALUES(?,?,?,1,NULL,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,version=version+1,deleted_at=NULL,updated_at=excluded.updated_at",
                (cid, "correction", s.dumps(values), stamp, stamp),
            )
            after_c = s.decode(
                db.execute("SELECT * FROM entities WHERE id=?", (cid,)).fetchone()
            )
            s.audit(db, cid, "correction", "rollback", old, after_c, "回滚手动新增价格")
            db.execute("UPDATE audit SET reverted_at=? WHERE id=?", (stamp, audit_id))
            return {"ok": True}
        current = s.decode(
            db.execute(
                "SELECT * FROM entities WHERE id=?", (record["entity_id"],)
            ).fetchone()
        )
        if not current:
            raise HTTPException(409, "对象已永久删除，无法回滚")
        if not body.get("force") and (
            s.payload(current) != s.payload(after or {})
            or current["deleted_at"] != (after or {}).get("deleted_at")
        ):
            raise HTTPException(409, "对象存在后续修改，请查看差异后选择是否覆盖")
        target = before or {**current, "deleted_at": s.now()}
        if (
            record["kind"] == "product"
            and current.get("deleted_at")
            and not target.get("deleted_at")
        ):
            target = {**target, "paused": True}
        restored = {**target, "version": current["version"] + 1, "updated_at": s.now()}
        db.execute(
            "UPDATE entities SET data=?,version=?,deleted_at=?,updated_at=? WHERE id=?",
            (
                s.dumps(s.payload(restored)),
                restored["version"],
                restored.get("deleted_at"),
                restored["updated_at"],
                current["id"],
            ),
        )
        s.audit(
            db,
            current["id"],
            record["kind"],
            "rollback",
            current,
            restored,
            "回滚 " + audit_id,
        )
        db.execute("UPDATE audit SET reverted_at=? WHERE id=?", (s.now(), audit_id))
    return {"ok": True}


@app.get("/api/trash")
def trash():
    result = []
    for kind in MODELS:
        for row in s.entities(kind, deleted=True):
            row["remaining_days"] = max(
                0,
                s.settings()["trash_days"]
                - (d.utcnow() - d.dt(row["deleted_at"])).days,
            )
            result.append(row)
    return sorted(result, key=lambda r: r["deleted_at"], reverse=True)


@app.post("/api/trash/{entity_id}/restore")
def restore(entity_id: str):
    item = required(entity_id, deleted=True)
    if not item["deleted_at"]:
        raise HTTPException(409, "记录不在回收站")
    if item["kind"] == "product" and any(
        product_key(p) == product_key(item) for p in s.entities("product")
    ):
        raise HTTPException(409, "商品库中已有相同商品，请先处理重复项")
    return s.update(
        entity_id,
        {"paused": True} if item["kind"] == "product" else {},
        deleted_at=None,
        reason="从回收站恢复",
    )


@app.delete("/api/trash/{entity_id}")
def purge(entity_id: str):
    item = required(entity_id, deleted=True)
    if not item["deleted_at"]:
        raise HTTPException(409, "只能永久删除回收站中的记录")
    s.permanent_delete(entity_id)
    return {"ok": True}


@app.post("/api/backup")
def make_backup():
    return {"filename": worker.backup()}


@app.get("/api/backups")
def backups():
    return [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted((s.data_dir() / "backups").glob("monitor-*.sqlite3"), reverse=True)
    ]


@app.get("/api/backups/{name}")
def download_backup(name: str):
    if (
        Path(name).name != name
        or not name.startswith("monitor-")
        or not name.endswith(".sqlite3")
    ):
        raise HTTPException(400, "无效文件名")
    path = s.data_dir() / "backups" / name
    if not path.is_file():
        raise HTTPException(404, "备份不存在")
    directory = tempfile.TemporaryDirectory(prefix="monitor-backup-download-")
    target = Path(directory.name) / "account.sqlite3"
    target.touch(mode=0o600)
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(target)) as saved:
                original.backup(saved)
                public_view.scrub_database(saved, include_runtime=s.TENANT.get() == "admin")
        return FileResponse(target, filename=name, media_type="application/octet-stream",
                            background=BackgroundTask(directory.cleanup))
    except BaseException:
        directory.cleanup()
        raise


def system_backup_path(name):
    import re
    if not re.fullmatch(r"guidajia-full-\d{8}-\d{6}-[a-f0-9]{8}\.gdbak", name):
        raise HTTPException(422, "无效的完整备份文件名")
    path = s.DATA / "system-backups" / name
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "完整备份不存在")
    return path


@app.get("/api/security/full-backups")
def system_backup_list():
    return [{"name": path.name, "size": path.stat().st_size} for path in sorted((s.DATA / "system-backups").glob("guidajia-full-*.gdbak"), reverse=True) if not path.is_symlink()]


@app.post("/api/security/full-backups")
async def system_backup_create(request: Request):
    body = await account_body(request)
    if type(body.get("include_cookies", False)) is not bool:
        raise HTTPException(422, "Cookie 备份选项无效")
    result = await asyncio.to_thread(full_backup.create, body.get("password"), body.get("include_cookies", False))
    s.log("管理员已创建密码加密的完整备份")
    return result


@app.get("/api/security/full-backups/{name}")
def system_backup_download(name: str):
    return FileResponse(system_backup_path(name), filename=name, media_type="application/octet-stream")


@app.post("/api/security/full-backups/{name}/check")
async def system_backup_check(name: str, request: Request):
    body = await account_body(request)
    return await asyncio.to_thread(full_backup.check, system_backup_path(name), body.get("password"))


@app.get("/api/export/{kind}")
def export(kind: str, format: str = "json", product_id: str | None = None):
    if format not in ("json", "csv"):
        raise HTTPException(422, "导出格式只支持 csv 或 json")
    if kind == "observations":
        data = [
            {**r, "price_unit": "cent", "price_basis": pd.price_basis(r)}
            for r in d.records(product_id)
        ]
    elif kind == "audit":
        data = audit_list()
    elif kind == "logs":
        data = logs(product_id)
    elif kind in MODELS:
        data = s.entities(kind)
    else:
        raise HTTPException(400, "不支持的导出类型")
    if kind in ("observations", "logs"):
        data = public_view.technical(data)
    if format == "csv":
        return Response(
            csv_io.render(kind, data),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{kind}.csv"'},
        )
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{kind}.json"'},
    )


@app.get("/api/import-template/{kind}")
def import_template(kind: str):
    if kind not in MODELS and kind != "observations":
        raise HTTPException(400, "不支持的导入类型")
    return Response(
        csv_io.render(kind, [], template=True),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{kind}-template.csv"'},
    )


@app.get("/api/import-guide")
def import_guide():
    return FileResponse(s.ROOT / "docs" / "CSV_FORMAT.md", filename="CSV_FORMAT.md")


@app.post("/api/import/{kind}")
def import_data(kind: str, body: dict):
    if kind not in MODELS and kind != "observations":
        raise HTTPException(400, "不支持的导入类型")
    text = body.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(422, "导入内容必须为文本")
    if len(text.encode("utf-8")) > 5_000_000:
        raise HTTPException(413, "导入文件不能超过 5 MB")
    try:
        text = text.lstrip("\ufeff")
        is_csv = not text.lstrip().startswith(("[", "{"))
        raw = csv_io.parse(kind, text) if is_csv else json.loads(text)
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or len(raw) > 1000:
            raise ValueError("单次最多导入 1000 条")
    except (ValueError, TypeError, csv.Error) as exc:
        raise HTTPException(422, f"文件格式错误：{exc}")
    output = []
    seen = set()
    existing_prices = {}
    for index, row in enumerate(raw):
        try:
            if not isinstance(row, dict):
                raise ValueError("每条记录应为对象")
            row = {k: v for k, v in row.items() if k not in s.META}
            if kind == "observations":
                product = required(row.get("product_id", ""), "product")
                if not csv_io.boolean(row.get("valid"), True) or (
                    row.get("status", "success") != "success"
                    and not csv_io.boolean(row.get("corrected"))
                ):
                    raise ValueError(
                        "失败或无效价格记录仅供导出查看，不作为真实价格导入"
                    )
                stamp = (
                    d.dt(row.get("observed_at", ""))
                    .astimezone(timezone.utc)
                    .isoformat()
                )
                if is_csv:
                    if (
                        "T" not in row.get("observed_at", "")
                        or datetime.fromisoformat(
                            row["observed_at"].replace("Z", "+00:00")
                        ).tzinfo
                        is None
                    ):
                        raise ValueError(
                            "observed_at 请用含时区的 ISO 8601，例如 2026-10-18T12:00:00+08:00"
                        )
                if d.dt(stamp) > d.utcnow() + timedelta(minutes=1):
                    raise ValueError("价格时间不能在未来")
                price = csv_io.amount(row.get("price"), row.get("price_unit", "yuan"))
                basis = row.get("price_basis") or (
                    "reference"
                    if not product.get("url")
                    else row.get("price_type") or "regular"
                )
                if basis not in pd.BASIS_NAMES:
                    raise ValueError(
                        "price_basis 只支持 regular / subsidy / coupon / member / reference"
                    )
                if not product.get("url") and basis != "reference":
                    raise ValueError(
                        "尚未填写淘宝链接的商品，请使用 reference（表格参考价）口径"
                    )
                context = product_urls.identity(product["url"])
                item_id = context["item_id"]
                sku_id = context["sku_id"]
                for key, expected in (
                    ("item_id", item_id),
                    ("requested_sku_id", sku_id),
                ):
                    if row.get(key) and (
                        pd.platform_id(row[key]) is None
                        or (
                            expected
                            and pd.platform_id(row[key]) != pd.platform_id(expected)
                        )
                    ):
                        raise ValueError(
                            f"{key} 与当前商品链接不一致，请先确认商品及 SKU"
                        )
                identity = {
                    "platform": context["platform"],
                    "item_id": row.get("item_id") or item_id,
                    "requested_sku_id": row.get("requested_sku_id") or sku_id,
                    "selected_sku_id": row.get("selected_sku_id"),
                    "sku_verified": csv_io.boolean(row.get("sku_verified")),
                    "source": "manual",
                    "price_basis": basis,
                }
                if basis == "reference":
                    if any(
                        row.get(k)
                        for k in ("item_id", "requested_sku_id", "selected_sku_id")
                    ):
                        raise ValueError(
                            "参考价不能同时填写淘宝商品 ID 或 SKU；请用对应的实际价格口径导入"
                        )
                    identity.update(
                        reference_product_id=product["id"],
                        item_id=None,
                        requested_sku_id=None,
                        selected_sku_id=None,
                        sku_verified=False,
                    )
                quality = row.get("data_quality") or "measured"
                if quality not in ("measured", "carried_forward"):
                    raise ValueError(
                        "data_quality 只支持 measured（实测）或 carried_forward（沿用前值）"
                    )
                if quality == "carried_forward" and csv_io.boolean(
                    row.get("corrected")
                ):
                    quality = "measured"
                if identity["selected_sku_id"] and not pd.platform_id(
                    identity["selected_sku_id"]
                ):
                    raise ValueError(
                        "selected_sku_id 必须为淘宝数字 SKU ID，请勿使用科学计数法"
                    )
                stock = row.get("stock") or "in_stock"
                if stock not in ("in_stock", "out_of_stock", "delisted", "unknown"):
                    raise ValueError(
                        "stock 只支持 in_stock / out_of_stock / delisted / unknown"
                    )
                if product["id"] not in existing_prices:
                    existing_prices[product["id"]] = {
                        (
                            d.dt(r["observed_at"]).astimezone(timezone.utc).isoformat(),
                            r["price"],
                            pd.price_basis(r),
                            pd.effective_sku(r),
                        )
                        for r in d.records(product["id"])
                        if pd.human_price(r)
                    }
                duplicate = (
                    stamp,
                    price,
                    basis,
                    pd.effective_sku(identity),
                ) in existing_prices[product["id"]]
                fingerprint = f'{row["product_id"]}:{stamp}:{price}:{basis}:{pd.effective_sku(identity)}'
                if duplicate or fingerprint in seen:
                    raise ValueError("价格观测已存在")
                original = row.get("original_price")
                output.append(
                    {
                        "row": index + 1,
                        "status": "new",
                        "data": {
                            "product_id": row["product_id"],
                            "observed_at": stamp,
                            "price": price,
                            "original_price": (
                                csv_io.amount(
                                    original,
                                    row.get("price_unit", "yuan"),
                                    "original_price",
                                )
                                if original not in (None, "")
                                else None
                            ),
                            "stock": stock,
                            "price_type": (
                                basis if basis in ("coupon", "member") else "regular"
                            ),
                            **identity,
                            "exclude_prediction": csv_io.boolean(
                                row.get("exclude_prediction")
                            ),
                            "data_quality": quality,
                            **{
                                k: str(row.get(k, ""))[:5000]
                                for k in ("import_source", "source_cell", "source_note")
                            },
                            "reason": row.get("reason", ""),
                        },
                    }
                )
                seen.add(fingerprint)
                continue
            for key in ("tags", "expected_options", "config", "channels"):
                if isinstance(row.get(key), str):
                    row[key] = (
                        json.loads(row[key])
                        if row[key]
                        else ([] if key != "config" else {})
                    )
            # Imported products start paused so a preview/import cannot flood the crawler.
            if kind == "product":
                row["paused"] = True
            value = validated(kind, row)
            fingerprint = product_key(value) if kind == "product" else s.dumps(value)
            if fingerprint in seen:
                raise ValueError("文件内重复记录")
            seen.add(fingerprint)
            output.append({"row": index + 1, "status": "new", "data": value})
        except (HTTPException, ValueError, TypeError) as exc:
            output.append(
                {
                    "row": index + 1,
                    "status": "skip",
                    "error": (
                        str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                    ),
                }
            )
    if not body.get("preview", True):
        for item in output:
            if item["status"] == "new":
                if kind == "observations":
                    values = item["data"]
                    d.save_observation(
                        values["product_id"],
                        {**values, "status": "success", "note": "文件导入"},
                        source="manual",
                        observed_at=values["observed_at"],
                    )
                else:
                    s.create(kind, item["data"], reason="文件导入")
    return {
        "rows": output,
        "new": sum(i["status"] == "new" for i in output),
        "skipped": sum(i["status"] == "skip" for i in output),
    }


@app.get("/api/forecast-review")
def forecast_review():
    grouped = {}
    with s.connection() as db:
        forecasts = [
            dict(r) for r in db.execute("SELECT * FROM forecasts ORDER BY created_at")
        ]
    config = s.settings()
    from . import prediction_data as pd

    series = pd.daily_series(
        d.records(), {**config, "include_coupon": True}, d.utcnow()
    )
    seen = set()
    for row in forecasts:
        prediction = json.loads(row["data"])
        price = prediction.get("current_price")
        # One sample per product per day prevents fast recrawls inflating metrics.
        series_key = prediction.get("series_key")
        # Old snapshots without immutable SKU/basis evidence are not silently
        # mixed into a newly calibrated evaluation.
        if not series_key:
            continue
        sample = (
            row["product_id"],
            pd.day(row["created_at"]),
            series_key,
            prediction.get("model_id", prediction.get("model")),
        )
        if sample in seen or not price:
            continue
        bars = series.get((row["product_id"], series_key), [])
        outcome = pd.future_outcome(
            bars, pd.day(row["created_at"]), prediction["window"], d.utcnow()
        )
        if not outcome:
            continue
        seen.add(sample)
        saving = (1 - outcome["minimum"] / price) * 100
        grouped.setdefault(prediction["label"], []).append(
            {
                "saving": saving,
                "drop": saving
                >= prediction.get("drop_threshold", config["drop_threshold"]),
                "probability": prediction.get("probability"),
                "interval": prediction.get("interval"),
                "minimum": outcome["minimum"],
            }
        )
    return [
        {
            "label": label,
            "samples": len(values),
            "drop_rate": round(
                sum(v["drop"] for v in values) / len(values) * 100,
                1,
            ),
            "saving": round(sum(v["saving"] for v in values) / len(values), 2),
            "probability_samples": sum(v["probability"] is not None for v in values),
            "brier": (
                round(
                    sum(
                        (v["probability"] / 100 - int(v["drop"])) ** 2
                        for v in values
                        if v["probability"] is not None
                    )
                    / sum(v["probability"] is not None for v in values),
                    5,
                )
                if any(v["probability"] is not None for v in values)
                else None
            ),
            "interval_coverage": (
                round(
                    sum(
                        v["interval"][0] <= v["minimum"] <= v["interval"][1]
                        for v in values
                        if v["interval"]
                    )
                    / sum(bool(v["interval"]) for v in values)
                    * 100,
                    1,
                )
                if any(v["interval"] for v in values)
                else None
            ),
        }
        for label, values in grouped.items()
    ]


@app.get("/api/stream")
async def stream(request: Request):
    async def events():
        while not await request.is_disconnected():
            if access.guard(request) is not None:
                return
            yield "data: " + json.dumps(
                {
                    "time": s.now(),
                    "health": public_view.technical(d.health()),
                    "session": collector.session_info(),
                },
                ensure_ascii=False,
            ) + "\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


DIST = s.ROOT / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        candidate = (DIST / path).resolve()
        if candidate.is_relative_to(DIST) and candidate.is_file():
            return FileResponse(candidate)
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        return FileResponse(DIST / "index.html")
