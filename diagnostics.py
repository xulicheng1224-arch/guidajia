"""Bounded crawler diagnostics containing product evidence, never cookies/headers."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

MESSAGES = {
    "api_not_configured": ("该平台的官方 API 尚未配置或启用", "进入设置 → 价格来源，填写当前账号的应用凭证并启用对应平台。"),
    "api_auth_error": ("官方 API 鉴权失败", "核对 AppKey、AppSecret、授权令牌和服务器时间。"),
    "api_permission_error": ("官方 API 应用没有所需权限", "在对应开放平台检查应用的接口权限和授权范围。"),
    "api_rate_limit": ("官方 API 调用达到平台频率或配额限制", "检查应用配额，降低统一采集频率后重试。"),
    "api_http_error": ("官方 API 网关返回 HTTP 错误", "查看 HTTP 状态，稍后重试；本次不会回退到爬虫。"),
    "api_network_error": ("无法连接官方 API 网关", "检查服务器网络后重试。"),
    "api_response_invalid": ("官方 API 响应格式不符合当前接口协议", "核对所选官方接口和开放平台文档。"),
    "api_empty": ("官方 API 未返回此商品", "检查商品 ID、推广范围及应用权限；没有商品时价格为 None。"),
    "api_error": ("官方 API 返回业务错误", "根据官方错误代码检查应用权限、商品和配置。"),
    "api_sku_required": ("该官方接口需要明确的 SKU", "在淘宝商品链接中填写 skuId，再调用 SKU 查询接口。"),
    "api_sku_unsupported": ("所选官方接口只提供商品级价格，不能核验指定 SKU", "如有卖家授权，可选择淘宝 SKU 查询接口；不会将商品级价格冒充规格价。"),
    "source_unsupported": ("当前爬虫分支不支持京东商品", "启用并配置京东官方 API，再切换到官方 API 分支。"),
    "success": ("采集完成", "无需处理。"),
    "price_region_missing": (
        "商品页面已打开，但没有找到可见的价格区域",
        "在 Mac 打开商品检查是否显示价格；确认已登录，必要时增加渲染等待时间后重试。",
    ),
    "price_missing": (
        "找到了价格区域，但无法提取唯一、完整的金额",
        "查看价格文本与解析证据；多价格区间或页面结构变化需要核对。",
    ),
    "price_masked": (
        "页面金额包含问号或隐藏字符，无法取得完整价格",
        "在 Mac 浏览器检查登录和验证状态；隐藏金额不会作为有效价格。",
    ),
    "sku_unverified": (
        "已读到页面价格，但尚未确认它属于指定 SKU",
        "编辑商品填写预期规格，或在 Mac 打开链接确认所选规格；不能只凭 URL 确认 SKU。",
    ),
    "sku_mismatch": (
        "页面当前规格与指定 SKU 不一致",
        "检查链接中的 skuId 和预期规格，修改为对应商品规格后重试。",
    ),
    "login_or_redirect": (
        "页面跳转到了登录页或其他非商品页面",
        "在运行服务的 Mac 上重新登录淘宝并保存会话，然后重试。",
    ),
    "blocked": (
        "页面要求验证或限制了访问",
        "在 Mac 上打开商品完成页面要求的验证，降低采集频率后重试。",
    ),
    "page_missing": ("商品页面提示商品不存在或已删除", "确认商品链接是否有效。"),
    "item_mismatch": (
        "页面商品 ID 与监测链接不一致",
        "核对链接，避免把跳转后的其他商品价格记入本商品。",
    ),
    "timeout": (
        "采集超过允许时间",
        "检查网络与浏览器状态，稍后重试或调整渲染等待时间。",
    ),
    "setup_error": (
        "采集浏览器或运行环境无法启动",
        "检查所选浏览器是否已安装；切换本机 Chrome / Edge 后重试。",
    ),
    "crawler_error": (
        "采集过程中发生异常",
        "根据下方失败阶段和错误代码检查网络、浏览器与会话，再重试。",
    ),
    "anomaly": (
        "价格偏离历史区间，需要人工确认",
        "核对商品规格及价格，在商品详情中确认异常价。",
    ),
}


def product_url(value):
    """Retain only public product IDs; login redirects may carry credentials."""
    try:
        u = urlsplit(value)
        if u.hostname not in ("item.taobao.com", "detail.tmall.com"):
            return u.hostname or ""
        query = parse_qs(u.query)
        allowed = {
            k: query[k][0]
            for k in ("id", "skuId")
            if query.get(k) and query[k][0].isdigit()
        }
        return urlunsplit(("https", u.hostname, u.path, urlencode(allowed), ""))
    except ValueError:
        return ""


def exception_detail(error, stage):
    code = re.search(r"\b(ERR_[A-Z0-9_]+)\b", str(error))
    text = str(error).lower()
    message = None
    if code:
        message = f"浏览器网络错误：{code.group(1)}"
    elif "executable doesn't exist" in text or "executable does not exist" in text:
        message = "找不到所选浏览器的可执行文件，请安装浏览器或切换浏览器"
    elif "timeout" in type(error).__name__.lower() or "timed out" in text:
        message = "等待页面加载或渲染超时"
    elif "closed" in type(error).__name__.lower():
        message = "浏览器、页面或上下文提前关闭"
    return {
        "stage": stage,
        "error_type": type(error).__name__,
        "error_code": code.group(1) if code else None,
        "exception_message": message,
    }


def describe(result):
    code = result.get("status", "crawler_error")
    message, suggestion = MESSAGES.get(code, (code, "查看解析证据后重试。"))
    info = dict(result.get("diagnostics") or {})
    info.update(code=code, message=message, suggestion=suggestion)
    if code == "price_region_missing" and info.get("render_state") == "blank":
        info.update(message="页面仍为空白，商品内容与价格区域尚未渲染",
                    suggestion="查看文档状态、正文长度及脚本/资源错误；HTTP 200 不代表商品内容已加载。")
    elif code == "price_region_missing" and info.get("body_text_length", 0) > 0:
        info.update(message="页面已有内容，但没有找到匹配的可见价格区域",
                    suggestion="核对页面是否为商品详情、是否显示完整价格，以及当前价格定位规则。")
    if code == "login_or_redirect" and info.get("cookie_required"):
        info.update(
            message="当前账号尚未上传自己的淘宝 Cookie，本次未启动爬虫",
            suggestion="进入设置 → 淘宝账号，在本设备登录淘宝并导出、上传 Cookie；管理员的 Mac 会话不会共享给普通用户。",
        )
    if code == "blocked" and info.get("verification_required"):
        kind = info.get("verification_kind")
        info["message"] = (
            "淘宝要求短信安全验证，本次认证未通过"
            if kind == "sms" else "淘宝要求人工安全验证"
        )
        info["suggestion"] = "在运行采集的 Mac 浏览器中由本人完成页面要求的验证并保存会话，再重试单个商品；重复上传 Cookie 不能代替验证。"
    if info.get("price_source") == "api":
        if code == "success":
            info.update(message="官方 API 价格获取完成", suggestion="商品级价格不冒充 SKU 价格；只有可确认商品与 SKU 的记录参与对应规格训练。")
        elif code in ("price_missing", "timeout", "sku_mismatch", "item_mismatch"):
            info["message"] = {"price_missing": "官方 API 没有返回有效金额", "timeout": "官方 API 请求超时", "sku_mismatch": "官方 API 返回的 SKU 与目标不一致", "item_mismatch": "官方 API 返回的商品 ID 与目标不一致"}[code]
            info["suggestion"] = "检查商品、接口及应用配置后重试，本次不会切换到爬虫。"
    if result.get("error_type"):
        info.setdefault("error_type", result["error_type"])
    for key in (
        "requested_sku_id",
        "selected_sku_id",
        "sku_verification",
        "selected_options",
        "expected_options",
        "title",
        "final_host",
        "selected_option_ids",
        "selected_prop_path",
    ):
        if key in result:
            info[key] = result[key]
    if "detected_price" not in info:
        for method in result.get("methods", {}).values():
            if method.get("sale_price"):
                info["detected_price"] = str(method["sale_price"])
                break
    if result.get("price") is not None and "detected_price" not in info:
        info["detected_price"] = str(result["price"])
    if code == "sku_unverified" and not info.get("detected_price"):
        info["message"] = "指定 SKU 未通过核验；旧记录未保留完整的原始失败原因"
        info["suggestion"] = "重新采集以获得失败阶段、价格区域和规格证据。"
    return info
