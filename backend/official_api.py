"""Signed requests to fixed official gateways; never fall back to a browser."""

import asyncio
import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from diagnostics import describe
from collection_limits import COLLECT_TIMEOUT_SECONDS
from . import collector, price_sources
from .product_urls import identity

GATEWAYS = {"taobao": "https://eco.taobao.com/router/rest", "jd": "https://api.jd.com/routerjson"}
TIMEOUT_SECONDS = COLLECT_TIMEOUT_SECONDS
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def signature(params, secret):
    plain = "".join(key + str(value) for key, value in sorted(params.items()) if key != "sign" and value not in (None, ""))
    return hashlib.md5((secret + plain + secret).encode("utf-8")).hexdigest().upper()


def failure(code, platform, method=None, **details):
    result = {"price": None, "status": code, "stock": "unknown", "platform": platform,
        "api_method": method, "diagnostics": {"stage": "api_request", "price_source": "api", "platform": platform, "api_method": method, **details}}
    result["diagnostics"] = describe(result)
    return result


def safe_text(value, config):
    text = str(value or "")
    for field in ("app_key", "app_secret", "token"):
        if config.get(field):
            text = text.replace(config[field], "[已隐藏]")
    text = re.sub(r"https?://\S+", "[接口地址]", text)
    return re.sub(r"[\x00-\x1f]", " ", text)[:400]


def parameters(info, config):
    platform, item, sku = info["platform"], info["item_id"], info["sku_id"]
    method = config["method"]
    params = {"method": method, "app_key": config["app_key"], "format": "json", "sign_method": "md5",
        "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"), "v": "2.0" if platform == "taobao" else "1.0"}
    if config.get("token"):
        params["session" if platform == "taobao" else "access_token"] = config["token"]
    if platform == "taobao":
        if method == "taobao.item.sku.get":
            if not sku:
                return None, "api_sku_required"
            params.update(num_iid=item, sku_id=sku, fields="sku_id,num_iid,price,quantity,properties_name,status")
        else:
            # This method returns an item-level price, never a selected SKU.
            if sku:
                return None, "api_sku_unsupported"
            params.update(num_iids=item, platform="1")
    else:
        business = {"skuIds": sku} if method.endswith("promotiongoodsinfo.query") else {"goodsReqDTO": {"skuIds": [int(sku)], "pageIndex": 1, "pageSize": 1}}
        params["360buy_param_json"] = json.dumps(business, ensure_ascii=False, separators=(",", ":"))
    params["sign"] = signature(params, config["app_secret"])
    return params, None


async def request_json(platform, params):
    # Fixed HTTPS endpoints, no redirects and no environment proxies: a user's
    # credentials are sent only to the selected platform's official gateway.
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=15), trust_env=False, follow_redirects=False) as client:
        async with client.stream("POST", GATEWAYS[platform], data=params) as response:
            if response.status_code != 200:
                return response.status_code, None
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("oversized_response")
            return 200, json.loads(data)


def api_error(data, config):
    code = safe_text(data.get("sub_code") or data.get("code") or "unknown", config)
    message = safe_text(data.get("sub_msg") or data.get("msg") or data.get("message") or data.get("zh_desc"), config)
    clue = (code + " " + message).lower()
    status = "api_error"
    if any(word in clue for word in ("permission", "权限", "unauthorized", "not-authorized", "accesscontrol")):
        status = "api_permission_error"
    elif any(word in clue for word in ("limit", "频率", "quota", "流量")):
        status = "api_rate_limit"
    elif any(word in clue for word in ("sign", "appkey", "app_key", "session", "token", "secret", "授权", "认证")) or code in ("25", "26", "27", "28", "29", "40", "41"):
        status = "api_auth_error"
    return status, {"error_code": code, "exception_message": message or "官方接口返回失败，请检查应用权限和配置"}


def parse(data, info, config):
    platform, method = info["platform"], config["method"]
    def fail(code, **details):
        return failure(code, platform, method, **details)
    if not isinstance(data, dict):
        return fail("api_response_invalid")
    error = data.get("error_response")
    if isinstance(error, dict):
        code, details = api_error(error, config)
        return fail(code, **details)
    field = method.removeprefix("taobao.").replace(".", "_") if platform == "taobao" else method.replace(".", "_")
    envelope = data.get(field + "_response", data.get(field + "_responce"))
    if not isinstance(envelope, dict):
        return fail("api_response_invalid")
    if str(envelope.get("code", "0")) not in ("0", "200"):
        code, details = api_error(envelope, config)
        return fail(code, **details)
    if platform == "taobao":
        if method == "taobao.item.sku.get":
            row = envelope.get("sku")
            if not isinstance(row, dict):
                return fail("api_empty")
            if str(row.get("num_iid", "")) != info["item_id"]:
                return fail("item_mismatch")
            if str(row.get("sku_id", "")) != info["sku_id"]:
                return fail("sku_mismatch")
            amount, original = row.get("price"), None
            title = ""
            selected = [part.rsplit(":", 1)[-1] for part in str(row.get("properties_name") or "").split(";") if part]
            stock = "out_of_stock" if str(row.get("quantity")) == "0" else "unknown"
        else:
            results = envelope.get("results") or {}
            rows = results.get("n_tbk_item", []) if isinstance(results, dict) else []
            rows = [rows] if isinstance(rows, dict) else rows
            if not isinstance(rows, list) or not rows:
                return fail("api_empty")
            row = next((row for row in rows if isinstance(row, dict) and str(row.get("num_iid")) == info["item_id"]), None)
            if row is None:
                return fail("item_mismatch")
            amount, original = row.get("zk_final_price"), row.get("reserve_price")
            title, selected, stock = row.get("title", ""), [], "unknown"
    else:
        nested = envelope.get("result") or envelope.get("queryResult")
        if isinstance(nested, str):
            nested = json.loads(nested)
        if not isinstance(nested, dict):
            return fail("api_response_invalid")
        if str(nested.get("code", "")) != "200":
            code, details = api_error(nested, config)
            return fail(code, **details)
        rows = nested.get("data") or []
        if not isinstance(rows, list) or not rows:
            return fail("api_empty")
        row = next((row for row in rows if isinstance(row, dict) and str(row.get("skuId")) == info["sku_id"]), None)
        if row is None:
            return fail("sku_mismatch")
        if method.endswith("promotiongoodsinfo.query"):
            amount = row.get("unitPrice")
        else:
            amount = (row.get("priceInfo") or {}).get("price")
        original = None
        title, selected, stock = row.get("goodsName") or row.get("skuName") or "", [], "unknown"
    price = collector.cents(amount) if not isinstance(amount, bool) else None
    if price is None:
        return fail("price_missing", stage="api_parse", exception_message="官方接口没有返回唯一有效的单品金额，不使用区间价或预估优惠价")
    verified = bool(info["sku_id"])
    result = {"price": price, "original_price": collector.cents(original), "status": "success", "stock": stock,
        "platform": platform, "item_id": info["item_id"], "requested_sku_id": info["sku_id"],
        "selected_sku_id": info["sku_id"] if verified else None, "sku_verified": verified,
        "sku_verification": "official_api" if verified else None, "selected_options": selected,
        "price_basis": "regular", "price_type": "regular", "api_method": method,
        "api_granularity": "sku" if verified else "item", "title": str(title)[:500],
        "diagnostics": {"stage": "api_complete", "price_source": "api", "platform": platform, "api_method": method,
            "http_status": 200, "detected_price": str(amount), "api_granularity": "sku" if verified else "item"}}
    result["diagnostics"] = describe(result)
    return result


async def collect(product, settings):
    info = identity(product["url"])
    platform = info["platform"]
    config = price_sources.profiles(settings)[platform]
    method = config["method"]
    if not config["enabled"] or not config["app_key"] or not config["app_secret"]:
        return failure("api_not_configured", platform, method)
    params, error = parameters(info, config)
    if error:
        return failure(error, platform, method)
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            status, data = await request_json(platform, params)
            if status != 200:
                code = "api_auth_error" if status in (401, 403) else "api_rate_limit" if status == 429 else "api_http_error"
                return failure(code, platform, method, http_status=status)
            return parse(data, info, config)
    except (TimeoutError, httpx.TimeoutException):
        return failure("timeout", platform, method, timeout_seconds=TIMEOUT_SECONDS)
    except (ValueError, TypeError, AttributeError):
        return failure("api_response_invalid", platform, method)
    except httpx.HTTPError as error:
        return failure("api_network_error", platform, method, error_type=type(error).__name__)
