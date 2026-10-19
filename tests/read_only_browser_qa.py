"""Isolated public login and read-only UI QA using synthetic credentials."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "https://readonly.test"
ADMIN = "synthetic-admin-token"


async def main():
    with tempfile.TemporaryDirectory(prefix="monitor-readonly-ui-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        folder = Path(temporary)
        (folder / "public-access.json").write_text(json.dumps({"origin": URL}))
        (folder / "access-token").write_text(ADMIN)
        from backend import app as server, access, domain as d, store as s

        s.init()
        access.rotate_read_only_token()
        reader = access.read_only_token()
        product = s.create(
            "product",
            server.validated(
                "product",
                {
                    "name": "只读权限测试显卡",
                    "url": "https://item.taobao.com/item.htm?id=123&skuId=101",
                    "paused": True,
                },
            ),
        )
        d.save_observation(
            product["id"],
            {
                "price": 12345,
                "status": "success",
                "stock": "in_stock",
                "sku_verified": True,
            },
        )
        errors, writes = [], []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app)
        ) as local:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True, **taobao_browser_options("chrome")
                )
                try:
                    page = await browser.new_page(
                        viewport={"width": 1280, "height": 900}
                    )
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    async def route_handler(route):
                        request = route.request
                        if not any(
                            request.url.startswith(origin + "/")
                            for origin in (URL, URL.replace("https://", "http://"))
                        ) or request.url.endswith("/api/stream"):
                            await route.abort()
                            return
                        if request.method not in ("GET", "HEAD"):
                            writes.append(
                                (request.method, request.url.removeprefix(URL))
                            )
                        # Reproduce Cloudflare terminating TLS, then connecting
                        # to the origin over HTTP. The browser keeps HTTPS.
                        edge_scheme = (
                            "https" if request.url.startswith(URL + "/") else "http"
                        )
                        request_headers = await request.all_headers()
                        request_headers.update(
                            {
                                "cf-visitor": json.dumps({"scheme": edge_scheme}),
                                "x-forwarded-proto": edge_scheme,
                            }
                        )
                        response = await local.request(
                            request.method,
                            request.url.replace("https://", "http://", 1),
                            headers=request_headers,
                            content=request.post_data_buffer,
                        )
                        headers = dict(response.headers)
                        headers.pop("content-length", None)
                        headers.pop("content-encoding", None)
                        await route.fulfill(
                            status=response.status_code,
                            headers=headers,
                            body=response.content,
                        )

                    await page.route("**/*", route_handler)
                    upgrade = await local.get(
                        URL.replace("https://", "http://") + "/products",
                        headers={"cf-visitor": '{"scheme":"http"}'},
                        follow_redirects=False,
                    )
                    assert upgrade.status_code == 308
                    assert upgrade.headers["location"] == URL + "/products"
                    await page.goto(URL + "/products", wait_until="domcontentloaded")
                    assert page.url == URL + "/products"
                    assert await page.title() == "登录 · 鬼打架"
                    image = page.locator(".brand img")
                    await image.wait_for()
                    assert await image.evaluate(
                        "image => image.complete && image.naturalWidth > 0"
                    )
                    brand_output = ROOT / "output" / "brand-qa"
                    brand_output.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(
                        path=str(brand_output / "login.png"), full_page=True
                    )
                    glass_output = ROOT / "output" / "glass-qa"
                    glass_output.mkdir(parents=True, exist_ok=True)
                    for mode in ("light", "dark"):
                        await page.emulate_media(color_scheme=mode)
                        for width in (1280, 390):
                            await page.set_viewport_size(
                                {"width": width, "height": 900}
                            )
                            await page.reload(wait_until="domcontentloaded")
                            await page.wait_for_timeout(700)
                            assert (
                                await page.evaluate(
                                    "document.documentElement.dataset.theme"
                                )
                                == mode
                            )
                            assert await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth + 2"
                            )
                            await page.screenshot(
                                path=str(glass_output / f"login-{mode}-{width}.png"),
                                full_page=True,
                            )
                    await page.emulate_media(color_scheme="light")
                    await page.set_viewport_size({"width": 1280, "height": 900})
                    await page.reload(wait_until="domcontentloaded")
                    await page.get_by_label("访问口令", exact=True).fill(reader)
                    await page.get_by_role("button", name="登录", exact=True).click()
                    await page.get_by_text("只读访问", exact=True).wait_for()
                    assert await page.title() == "鬼打架 · 硬件价格监测"
                    assert await page.locator(".sidebar .brand img").evaluate(
                        "image => image.complete && image.naturalWidth > 0"
                    )
                    assert (
                        await page.get_by_role(
                            "button", name="添加商品", exact=True
                        ).count()
                        == 0
                    )
                    await page.get_by_role(
                        "link", name=product["name"], exact=True
                    ).click()
                    await page.get_by_role(
                        "heading", name=product["name"], exact=True
                    ).wait_for()
                    await page.get_by_role("tab", name="每日价格", exact=True).click()
                    await page.get_by_text("¥ 123.45", exact=True).first.wait_for()
                    for name in (
                        "编辑",
                        "立即重抓",
                        "设置提醒",
                        "编辑当日价格",
                        "手动新增价格",
                    ):
                        assert (
                            await page.get_by_role(
                                "button", name=name, exact=True
                            ).count()
                            == 0
                        ), name
                    await page.get_by_role("tab", name="价格记录", exact=True).click()
                    await page.get_by_text("爬虫采集", exact=True).wait_for()
                    api_check = await page.evaluate("""async () => {
                        const response = await fetch('/api/settings', {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({global_paused:true})});
                        const secrets = await fetch('/api/security/token');
                        return {write:response.status,secrets:secrets.status};
                    }""")
                    assert api_check == {"write": 403, "secrets": 403}
                    assert s.settings()["global_paused"] is False
                    output = ROOT / "output" / "read-only-qa"
                    output.mkdir(parents=True, exist_ok=True)
                    for width in (1280, 390):
                        await page.set_viewport_size({"width": width, "height": 900})
                        await page.wait_for_timeout(350)
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 2"
                        )
                        await page.screenshot(
                            path=str(output / f"viewer-product-{width}.png"),
                            full_page=True,
                        )
                    await page.goto(URL + "/forecasts", wait_until="domcontentloaded")
                    await page.get_by_role(
                        "heading", name="预测中心", exact=True
                    ).wait_for()
                    assert await page.get_by_role(
                        "switch", name="启用学习模型"
                    ).is_disabled()
                    assert await page.get_by_role(
                        "button", name="训练并回测", exact=True
                    ).is_disabled()
                    await page.goto(
                        URL + "/settings?tab=security", wait_until="domcontentloaded"
                    )
                    await page.get_by_role(
                        "heading", name="此页面需要管理员权限", exact=True
                    ).wait_for()
                    assert (
                        await page.get_by_role(
                            "button", name="重置令牌", exact=True
                        ).count()
                        == 0
                    )
                    await page.get_by_role(
                        "button", name="退出登录", exact=True
                    ).click()
                    await page.get_by_role(
                        "heading", name="登录你的工作空间", exact=True
                    ).wait_for()
                    await page.get_by_label("访问口令", exact=True).fill(ADMIN)
                    await page.get_by_role("button", name="登录", exact=True).click()
                    await page.get_by_role(
                        "heading", name="设置", exact=True
                    ).wait_for()
                    # The unchanged admin token still receives the original UI.
                    await page.goto(
                        URL + "/products/" + product["id"],
                        wait_until="domcontentloaded",
                    )
                    await page.get_by_role(
                        "heading", name=product["name"], exact=True
                    ).wait_for()
                    assert (
                        await page.get_by_role(
                            "button", name="编辑", exact=True
                        ).count()
                        >= 1
                    )
                    assert server.TOKEN_FILE.read_text() == ADMIN
                    assert not errors, errors
                    assert all(
                        path
                        in ("/api/access/login", "/api/access/logout", "/api/settings")
                        for _, path in writes
                    ), writes
                    print(
                        json.dumps(
                            {
                                "viewer_login": True,
                                "http_upgrade": True,
                                "http_tunnel": True,
                                "viewer_reads": True,
                                "writes_denied": True,
                                "admin_preserved": True,
                                "widths": [1280, 390],
                                "page_errors": errors,
                            }
                        )
                    )
                finally:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
