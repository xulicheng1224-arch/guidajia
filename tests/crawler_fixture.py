"""Exercise the actual Mac browser + Crawlee handler using an intercepted DOM.

No external product request, no user Cookie, and no production database writes.
"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import crawler
from crawlee.crawlers import PlaywrightCrawler


async def exercise(scenario):
    script_headers = []
    price = "4???" if scenario == "masked" else "4,999.90"
    markup = f"""<!doctype html><html><head><meta charset="UTF-8"><title>浏览器采集验证</title></head><body>
    <div class="normalPrice--fixture">到手价 ¥ {price} 优惠前 ¥ 6999</div>
    <div class="skuItem selected">16GB</div>
    </body></html>"""
    if scenario == "missing":
        markup = "<html><body>商品不存在</body></html>"
    if scenario == "product_captcha":
        markup = "<html><head><title>验证码拦截</title></head><body>请完成验证，拖动滑块继续访问</body></html>"
    if scenario == "late_captcha":
        markup = "<html><head><script>setTimeout(()=>{document.title='验证码拦截';document.body.innerText='请完成验证，拖动滑块继续访问'},500)</script></head><body></body></html>"
    if scenario == "delayed_price":
        markup = "<html><head><title>延迟渲染商品</title><script>setTimeout(()=>{document.body.innerHTML='<div class=\"normalPrice--fixture\">到手价 ¥ 4999.90</div>'},31000)</script></head><body></body></html>"
    if scenario == "blank_script_failure":
        markup = "<html><head><script src='https://assets.example/boot.js?token=PRIVATE'></script></head><body></body></html>"
    if scenario == "native_headers":
        markup = markup.replace('</head>', '<script src="https://assets.example/native.js"></script></head>')
    if scenario in ("login_redirect", "login_handoff", "sms_verification"):
        markup = """<html><head><script>location.replace('https://login.taobao.com/havanaone/login/login.htm')</script></head><body>登录中</body></html>"""
    if scenario in ("sku_panel", "sku_mismatch", "sku_unknown"):
        markup = """<html><head><meta charset="utf-8"><title>新版 SKU 面板</title></head><body>
        <div id="tbpcDetail_SkuPanelBody"><div class="block2--fixture"><div><span class="unit--fixture">￥</span><span>3929.36</span></div></div></div>
        <div class="valueItem--fixture isSelected--fixture" data-vid="41420">8GB</div>
        <div class="valueItem--fixture isSelected--fixture" data-vid="38344584682">RTX5060 DUO 8G</div>
        <script>window.__ICE_APP_CONTEXT__={loaderData:{home:{data:{res:{skuBase:{props:[{pid:'30308',values:[{vid:'41420'}]},{pid:'1627207',values:[{vid:'38344584682'}]}],skus:[{skuId:'6309307487006',propPath:'30308:41420;1627207:38344584682'}]}}}}}};</script>
        </body></html>"""
        if scenario == "sku_unknown":
            markup = markup.replace(
                "window.__ICE_APP_CONTEXT__", "window.unused_metadata"
            )

    class FixtureCrawler(PlaywrightCrawler):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

            @self.pre_navigation_hook
            async def intercept(ctx):
                async def fulfill(route):
                    if "assets.example" in route.request.url:
                        if scenario == "native_headers":
                            headers = await route.request.all_headers()
                            assert headers.get('accept') == '*/*', 'Script requests must use the browser native Accept header'
                            script_headers.append(headers)
                            await route.fulfill(content_type='application/javascript',body='window.fixtureNativeHeaders=true')
                            return
                        await route.abort("failed")
                        return
                    if "login.taobao.com" in route.request.url:
                        login_markup = "<html><title>淘宝登录</title><body>请登录</body></html>"
                        if scenario == "sms_verification":
                            login_markup = "<html><title>身份验证</title><body>账号异常，请输入短信验证码完成验证。</body></html>"
                        if scenario == "login_handoff":
                            login_markup = """<html><body>登录中转<script>setTimeout(() => location.replace('https://detail.tmall.com/item.htm?id=123456'), 750)</script></body></html>"""
                        await route.fulfill(
                            status=200,
                            content_type="text/html; charset=utf-8",
                            body=login_markup,
                        )
                        return
                    if scenario == "login_handoff" and "detail.tmall.com" in route.request.url:
                        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body='<html><body><div class="normalPrice--fixture">到手价 ¥ 4999.90</div></body></html>')
                        return
                    await route.fulfill(
                        status=200, content_type="text/html; charset=utf-8", body=markup
                    )

                await ctx.page.route("**/*", fulfill)

    crawler.PlaywrightCrawler = FixtureCrawler
    with tempfile.TemporaryDirectory(prefix="silicon-dom-test-") as folder:
        output = Path(folder) / "result.json"
        args = argparse.Namespace(
            url="https://item.taobao.com/item.htm?id=123456"
            + (
                "&skuId=" + ("999" if scenario == "sku_mismatch" else "6309307487006")
                if scenario.startswith("sku_")
                else ""
            ),
            login=False,
            headed=False,
            browser="msedge",
            wait=0,
            state=Path(folder) / "auth.json",
            output=output,
        )
        if scenario == "blank_script_failure":
            args.timeout = 10
        if scenario == "cookie_roundtrip":
            from backend.session_state import normalize_uploaded
            base = {"name": "fixture", "value": "ordinary", "domain": ".taobao.com", "path": "/", "secure": True, "httpOnly": True, "sameSite": "None", "expires": -1}
            incoming = {"cookies": [base,
                {**base, "value": "cross-site", "partitionKey": "https://tmall.com", "_crHasCrossSiteAncestor": True},
                {**base, "value": "same-site", "partitionKey": "https://tmall.com", "_crHasCrossSiteAncestor": False}],
                "origins": [{"origin": "https://item.taobao.com", "localStorage": [{"name": "login-fixture", "value": "fixture-local"}]}]}
            normalized, rejected = normalize_uploaded(json.dumps(incoming))
            assert rejected == 0
            args.state.write_text(json.dumps(normalized))
        await crawler.crawl(args)
        result = json.loads(output.read_text())
        if scenario in ("success", "delayed_price", "native_headers"):
            assert result["status"] == "success", result
            assert result["price"] == "4999.90", result
            if scenario in ("success", "native_headers"):
                assert result["original_price"] == "6999", result
            assert result["session_state_saved"]
            if scenario == 'native_headers':
                assert len(script_headers) == 1
        elif scenario == "late_captcha":
            assert result["status"] == "blocked", result
            assert result["diagnostics"]["verification_required"]
            assert result["diagnostics"]["title"] == "验证码拦截"
            assert result["diagnostics"]["duration_ms"] < 10000
        elif scenario == "blank_script_failure":
            assert result["price"] is None and result["status"] in ("price_region_missing","timeout"),result
            assert result["diagnostics"]["render_state"] == "blank",result
            assert result["diagnostics"]["body_text_length"] == 0,result
            assert any(row['host']=='assets.example' for row in result['diagnostics']['request_failures']),result
            assert 'PRIVATE' not in str(result)
        elif scenario == "masked":
            assert (
                result["status"] == "price_masked" and result["price"] is None
            ), result
        elif scenario == "missing":
            assert (
                result["status"] == "page_missing" and result["price"] is None
            ), result
        elif scenario == "login_redirect":
            assert result["status"] == "login_or_redirect", result
            assert result["price"] is None
            assert result["diagnostics"]["login_detected"] is True
        elif scenario == "login_handoff":
            assert result["status"] == "success", result
            assert result["diagnostics"]["login_handoff_completed"] is True
        elif scenario == "sms_verification":
            assert result["status"] == "blocked", result
            assert result["diagnostics"]["verification_kind"] == "sms"
            assert result["diagnostics"]["stage"] == "security_verification"
            assert result["price"] is None
        elif scenario == "product_captcha":
            assert result["status"] == "blocked" and result["price"] is None, result
            assert result["diagnostics"]["verification_required"]
            assert result["diagnostics"]["verification_kind"] == "slider"
            assert result["diagnostics"]["stage"] == "security_verification"
        elif scenario == "cookie_roundtrip":
            assert result["status"] == "success", result
            restored = json.loads(args.state.read_text())
            cookies = [c for c in restored["cookies"] if c["name"] == "fixture"]
            assert len(cookies) == 3, cookies
            assert {c.get("_crHasCrossSiteAncestor") for c in cookies if c.get("partitionKey")} == {True, False}
            origin = next(o for o in restored["origins"] if o["origin"] == "https://item.taobao.com")
            assert {v["name"]: v["value"] for v in origin["localStorage"]}["login-fixture"] == "fixture-local"
        elif scenario.startswith("sku_"):
            from backend.collector import finalize_result

            result = finalize_result(result, {})
            if scenario == "sku_panel":
                assert (
                    result["status"] == "success" and result["price"] == 392936
                ), result
                assert result["sku_verification"] == "page_sku_mapping"
            else:
                assert result["price"] is None, result
                assert result["status"] == (
                    "sku_mismatch" if scenario == "sku_mismatch" else "sku_unverified"
                ), result
        print("DOM FIXTURE PASS", scenario)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario",
        choices=[
            "success",
            "masked",
            "missing",
            "sku_panel",
            "sku_mismatch",
            "sku_unknown",
            "login_redirect",
            "login_handoff",
            "sms_verification",
            "product_captcha",
            "late_captcha",
            "delayed_price",
            "blank_script_failure",
            "native_headers",
            "cookie_roundtrip",
        ],
    )
    asyncio.run(exercise(parser.parse_args().scenario))
