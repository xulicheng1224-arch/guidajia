"""Browser auth QA using a temporary database/token and in-process ASGI only.

All browser requests are intercepted. No credentials or requests reach a public
server, and no production data or credentials are read.
"""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "https://monitor.test"
QA_TOKEN = "temporary-qa-token"


async def main():
    with tempfile.TemporaryDirectory(prefix="monitor-access-qa-") as temp:
        os.environ["MONITOR_DATA_DIR"] = temp
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        (Path(temp) / "access-token").write_text(QA_TOKEN)
        (Path(temp) / "public-access.json").write_text(json.dumps({"origin": URL}))
        from backend import app as server, store

        store.init()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app)
        ) as local:
            await exercise(local)


async def exercise(local):
    folder = ROOT / "output" / "public-qa"
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            **taobao_browser_options("chrome"), headless=True
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def intercept(route):
            request = route.request
            if not request.url.startswith(URL + "/") or request.url.endswith(
                "/api/stream"
            ):
                await route.abort()
                return
            response = await local.request(
                request.method,
                request.url,
                headers=await request.all_headers(),
                content=request.post_data_buffer,
            )
            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers.pop("content-encoding", None)
            await route.fulfill(
                status=response.status_code, headers=headers, body=response.content
            )

        await page.route("**/*", intercept)
        await page.goto(URL + "/settings?tab=security", wait_until="domcontentloaded")
        await page.get_by_role("heading", name="登录你的工作空间").wait_for()
        await page.screenshot(path=str(folder / "login-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(folder / "login-mobile.png"), full_page=True)
        await page.set_viewport_size({"width": 1440, "height": 960})
        await page.get_by_label("访问口令", exact=True).fill(QA_TOKEN)
        await page.get_by_role("button", name="登录", exact=True).click()
        await page.get_by_role("heading", name="设置", exact=True).wait_for()
        await page.get_by_text("公网口令登录已启用", exact=True).wait_for()
        assert await page.get_by_role(
            "button", name=re.compile(r"^显\s*示$")
        ).is_disabled()
        await page.screenshot(path=str(folder / "security.png"), full_page=True)
        for path, title in (
            ("/", "总览看板"),
            ("/products", "商品库"),
            ("/collection", "采集管理"),
        ):
            await page.goto(URL + path, wait_until="domcontentloaded")
            await page.get_by_role("heading", name=title, exact=True).wait_for()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth"
            )
            print("PUBLIC PAGE OK", path, flush=True)
        state = await page.evaluate("fetch('/api/state').then(r => r.json())")
        assert state["access"]["remote"]
        cookies = await page.context.cookies()
        assert not any(c["name"] == "monitor_session" for c in cookies)
        assert any(
            c["name"] == "monitor_remote_session" and c["secure"] and c["httpOnly"]
            for c in cookies
        )
        await page.get_by_role("button", name="退出登录", exact=True).click()
        await page.get_by_role("heading", name="登录你的工作空间").wait_for()
        assert await page.evaluate("fetch('/api/state').then(r => r.status)") == 401
        assert not errors, errors
        await browser.close()
    (folder / "report.json").write_text(
        json.dumps(
            {
                "origin": URL,
                "login": True,
                "logout": True,
                "pages": 4,
                "mobile_width": 390,
                "page_errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("PUBLIC LOGIN / MOBILE / LOGOUT OK", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
