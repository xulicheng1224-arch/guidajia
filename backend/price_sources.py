"""Per-account, mutually exclusive browser/official-API collection configuration."""

import copy

from fastapi import HTTPException

from . import store as s

PROFILES = {
    "taobao": {"enabled": False, "app_key": "", "app_secret": "", "token": "", "method": "taobao.tbk.item.info.get"},
    "jd": {"enabled": False, "app_key": "", "app_secret": "", "token": "", "method": "jd.union.open.goods.promotiongoodsinfo.query"},
}
METHODS = {
    "taobao": {"taobao.tbk.item.info.get", "taobao.item.sku.get"},
    "jd": {"jd.union.open.goods.promotiongoodsinfo.query", "jd.union.open.goods.query"},
}


def mode(config=None):
    return (config or s.settings()).get("price_source", "crawler")


def profiles(config=None):
    values = (config or s.settings()).get("price_apis", {})
    return {name: {**defaults, **values.get(name, {})} for name, defaults in PROFILES.items()}


def public(config=None):
    config = config or s.settings()
    result = profiles(config)
    for value in result.values():
        value["configured"] = bool(value["app_key"] and value["app_secret"])
        for field in ("app_secret", "token"):
            value[field] = "__saved__" if value[field] else ""
    return {"mode": mode(config), "providers": result, "revision": config.get("price_source_revision", 0)}


def save(body):
    if not isinstance(body, dict) or set(body) - {"mode", "providers"}:
        raise HTTPException(422, "价格来源配置格式无效")
    with s.LOCK:
        config = s.settings()
        selected = body.get("mode", mode(config))
        if selected not in ("crawler", "api"):
            raise HTTPException(422, "请选择爬虫或官方 API 来源")
        updates = body.get("providers", {})
        if not isinstance(updates, dict) or set(updates) - PROFILES.keys():
            raise HTTPException(422, "只支持淘宝和京东官方 API")
        values = copy.deepcopy(profiles(config))
        for platform, changes in updates.items():
            if not isinstance(changes, dict) or set(changes) - set(PROFILES[platform]):
                raise HTTPException(422, "API 配置字段无效")
            for key, value in changes.items():
                if key == "enabled":
                    if type(value) is not bool:
                        raise HTTPException(422, "API 启用状态必须为布尔值")
                elif not isinstance(value, str) or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
                    raise HTTPException(422, "API 凭证或方法格式无效")
                if key in ("app_secret", "token") and value == "__saved__":
                    continue
                values[platform][key] = value.strip() if isinstance(value, str) else value
            if values[platform]["method"] not in METHODS[platform]:
                raise HTTPException(422, "请选择已支持的官方商品查询方法")
            value = values[platform]
            if value["enabled"] and (not value["app_key"] or not value["app_secret"]):
                raise HTTPException(422, "启用 API 前请填写 AppKey 和 AppSecret")
            if value["enabled"] and value["method"] == "taobao.item.sku.get" and not value["token"]:
                raise HTTPException(422, "淘宝 SKU 查询需要卖家授权 Session")
        if selected == "api" and not any(value["enabled"] for value in values.values()):
            raise HTTPException(422, "请先配置并启用至少一个平台的官方 API")
        changed = selected != mode(config) or values != profiles(config)
        if changed:
            s.set_settings({"price_source": selected, "price_apis": values,
                "price_source_revision": config.get("price_source_revision", 0) + 1,
                "failures": 0, "circuit_until": None})
            if selected == "api":
                with s.connection() as db:
                    db.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE status='queued' AND json_extract(context,'$.purpose')='cookie_verification'", (s.now(),))
                    db.execute("UPDATE jobs SET context=json_remove(context,'$.verify_session') WHERE status='queued' AND json_extract(context,'$.verify_session')=1")
            s.log("价格来源已切换为官方 API" if selected == "api" else "价格来源已切换为淘宝爬虫")
        return public()


def supported(product, config=None):
    from .product_urls import identity
    info = identity(product.get("url", ""))
    return profiles(config)[info["platform"]]["enabled"] if mode(config) == "api" else info["platform"] == "taobao"
