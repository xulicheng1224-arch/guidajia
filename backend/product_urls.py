"""Canonical product identity shared by manual, browser and official API prices."""

import re
from urllib.parse import parse_qs, urlencode, urlsplit

TAOBAO_HOSTS = {"item.taobao.com", "detail.tmall.com"}
JD_HOSTS = {"item.jd.com", "item.m.jd.com"}


def normalize_url(value):
    u = urlsplit(value.strip())
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        raise ValueError("请使用淘宝、天猫或京东的 HTTPS 商品详情链接")
    query = parse_qs(u.query)
    if u.hostname in JD_HOSTS:
        match = re.fullmatch(r"/(?:product/)?([0-9]+)\.html", u.path)
        if not match or not int(match[1]):
            raise ValueError("京东链接格式为 https://item.jd.com/数字SKU.html")
        return f"https://item.jd.com/{int(match[1])}.html"
    if u.hostname not in TAOBAO_HOSTS:
        raise ValueError("只支持淘宝、天猫或京东的商品详情链接")
    if not query.get("id") or not query["id"][0].isdigit() or not int(query["id"][0]):
        raise ValueError("商品链接必须包含数字 id 参数，请粘贴浏览器完整链接")
    params = {"id": str(int(query["id"][0]))}
    if query.get("skuId"):
        if not query["skuId"][0].isdigit() or not int(query["skuId"][0]):
            raise ValueError("skuId 必须为正整数")
        params["skuId"] = str(int(query["skuId"][0]))
    return f"https://{u.hostname}/item.htm?{urlencode(params)}"


def identity(value):
    if not value:
        return {"platform": "taobao", "item_id": None, "sku_id": None}
    canonical = normalize_url(value)
    u = urlsplit(canonical)
    if u.hostname == "item.jd.com":
        sku = u.path[1:-5]
        return {"platform": "jd", "item_id": sku, "sku_id": sku}
    query = parse_qs(u.query)
    return {"platform": "taobao", "item_id": query["id"][0], "sku_id": query.get("skuId", [None])[0]}
