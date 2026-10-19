"""One real product crawl verifies only the current tenant's saved session."""

import json
import secrets

from crawler import save_json
from diagnostics import describe
from . import store as s, session_state
from .product_urls import identity, normalize_url

CHECK_PRODUCT_ID = "__cookie_verification__"


def target(job):
    options = json.loads(job.get("context") or "{}")
    value = options.get("verification_target")
    if options.get("purpose") != "cookie_verification" or not isinstance(value, dict):
        return None
    url = normalize_url(value.get("url", ""))
    if identity(url)["platform"] != "taobao":
        raise ValueError("Cookie 校验仅支持淘宝或天猫链接")
    return {"id": CHECK_PRODUCT_ID, "name": "Cookie 校验", "url": url,
            "paused": False, "version": 0, "expected_options": []}


def enqueue_random(path):
    """Borrow only a public URL; retain the caller's tenant, queue and Cookie."""
    if not path.is_file():
        raise ValueError("请先上传当前账号自己的淘宝 Cookie")
    with s.LOCK:
        with s.connection() as db:
            old = db.execute("SELECT * FROM jobs WHERE product_id=? AND status IN ('queued','running')", (CHECK_PRODUCT_ID,)).fetchone()
        product = target(dict(old)) if old else None
        if product is None:
            urls = []
            with s.tenant_scope("admin"):
                for item in s.entities("product"):
                    try:
                        url = normalize_url(item.get("url", ""))
                        if identity(url)["platform"] == "taobao":
                            urls.append(url)
                    except (ValueError, TypeError, AttributeError):
                        continue
            if not urls:
                raise ValueError("管理员暂未配置可用于校验的淘宝或天猫商品链接，请联系管理员添加")
            product = {"id": CHECK_PRODUCT_ID, "name": "Cookie 校验", "url": secrets.choice(urls),
                       "paused": False, "version": 0, "expected_options": []}
        return enqueue(product, path, external=True)


def write(path, value):
    target = path.with_name("verification.json")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    save_json(target, value)
    target.chmod(0o600)


def invalidate(path):
    write(path, {"state": "not_checked", "message": "会话已更新，等待实际商品校验", "updated_at": s.now()})


def read(path):
    try:
        value = json.loads(path.with_name("verification.json").read_text())
    except (OSError, ValueError):
        value = {"state": "not_checked", "message": "尚未进行实际商品校验"}
    if value.get("job_id") and value.get("state") in ("queued", "running"):
        with s.connection() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (value["job_id"],)).fetchone()
        if not row or row[0] == "cancelled":
            value.update(state="cancelled", message="校验任务已取消，可重新校验")
        elif row[0] in ("success", "failed"):
            value.update(state="indeterminate", message="校验任务已结束但未保存有效校验结论，请重新校验")
    return value


def enqueue(product, path, external=False):
    if not path.is_file():
        raise ValueError("请先上传当前账号的淘宝 Cookie 或保存管理员登录会话")
    with s.connection() as db:
        old = db.execute("SELECT * FROM jobs WHERE product_id=? AND status IN ('queued','running')", (product["id"],)).fetchone()
        if old:
            job_id, state = old["id"], old["status"]
            # A queued ordinary task remains a real collection; mark it for
            # manual verification so it can run while automatic work is paused.
            db.execute("UPDATE jobs SET context=json_set(context,'$.verify_session',1) WHERE id=?", (job_id,))
        else:
            job_id, state = s.uid(), "queued"
            context = {"purpose": "cookie_verification", "verify_session": True,
                       "manual": True,
                       "allow_paused": product["paused"], "pause_version": product["version"]}
            if external:
                context["verification_target"] = {"url": product["url"]}
            db.execute("INSERT INTO jobs(id,product_id,status,created_at,context) VALUES(?,?,'queued',?,?)", (job_id, product["id"], s.now(), s.dumps(context)))
    value = {"job_id": job_id, "product_id": None if external else product["id"], "product_name": product["name"],
             "generation": session_state.generation(path),
             "state": state, "message": "校验已加入当前账号队列" if state == "queued" else "正在使用当前账号会话校验商品", "updated_at": s.now()}
    write(path, value)
    return value


def begin(path, job_id):
    value = read(path)
    if value.get("job_id") == job_id and value["state"] in ("queued", "running"):
        write(path, {**value, "state": "running", "message": "正在实际打开商品并读取价格", "updated_at": s.now()})


def finish(path, job_id, result, generation=None):
    value = read(path)
    if value.get("job_id") != job_id or value["state"] not in ("queued", "running"):
        return None
    if generation is not None and (value.get("generation") != generation or session_state.generation(path) != generation):
        write(path, {"state": "not_checked", "message": "任务执行期间会话已更新，请重新校验当前 Cookie", "updated_at": s.now()})
        return None
    code = result["status"]
    if code == "success" and result.get("price") is not None:
        state, message = "passed", "校验通过：使用当前账号的会话成功读取商品价格"
    elif code == "login_or_redirect":
        state, message = "login_required", "校验未通过：跳转登录页，请更新当前账号的 Cookie"
    elif code == "blocked":
        state, message = "verification_required", "校验未通过：淘宝要求验证或限制访问"
    else:
        state, message = "indeterminate", "暂时无法确认 Cookie 是否有效，请查看商品或解析错误详情"
    value.update(state=state, message=message, checked_at=s.now(), updated_at=s.now(), code=code, diagnostics=describe(result))
    write(path, value)
    return state
