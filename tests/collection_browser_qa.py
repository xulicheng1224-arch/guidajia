"""Verify collection controls with the built frontend and an isolated database."""

import asyncio
import os
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"


async def main():
    with tempfile.TemporaryDirectory(prefix="collection-ui-qa-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        from backend import app as server, access, store as s

        s.init()
        access.rotate_read_only_token()
        errors = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        ) as local:
            await local.get(URL + "/api/bootstrap")
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, **taobao_browser_options("chrome")
                )
                try:
                    page = await browser.new_page(
                        viewport={"width": 1280, "height": 900}
                    )
                    page.on("pageerror", lambda e: errors.append(str(e)))

                    async def intercept(route):
                        request = route.request
                        if not request.url.startswith(
                            URL + "/"
                        ) or request.url.endswith("/api/stream"):
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
                            status=response.status_code,
                            headers=headers,
                            body=response.content,
                        )

                    await page.route("**/*", intercept)
                    await page.goto(URL + "/collection", wait_until="domcontentloaded")
                    field = page.get_by_label("最大并发采集数", exact=True)
                    await field.wait_for()
                    assert await field.input_value() == "5"
                    await page.get_by_text(
                        "正在采集 0 / 5 · 等待 0", exact=True
                    ).wait_for()
                    await page.get_by_text(
                        "最多同时采集 5 个商品；每次采集限时 60 秒，超时强制终止且不自动重试。",
                        exact=True,
                    ).wait_for()
                    await field.fill("2")
                    await page.get_by_role(
                        "button", name="保存采集策略", exact=True
                    ).click()
                    await page.get_by_text("采集策略已保存", exact=True).wait_for()
                    assert s.settings()["max_concurrency"] == 2
                    await page.reload(wait_until="domcontentloaded")
                    await field.wait_for()
                    assert await field.input_value() == "2"
                    await page.get_by_text(
                        "正在采集 0 / 2 · 等待 0", exact=True
                    ).wait_for()
                    await page.get_by_role(
                        "button", name="恢复默认值", exact=True
                    ).click()
                    assert await field.input_value() == "5"
                    await page.get_by_role(
                        "button", name="保存采集策略", exact=True
                    ).click()
                    await page.get_by_text("采集策略已保存", exact=True).wait_for()
                    assert s.settings()["max_concurrency"] == 5
                    output = ROOT / "output/collection-qa"
                    output.mkdir(parents=True, exist_ok=True)
                    for width in (1280, 390):
                        await page.set_viewport_size({"width": width, "height": 900})
                        await page.wait_for_timeout(250)
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 2"
                        ), width
                        await page.screenshot(
                            path=str(output / f"collection-{width}.png"), full_page=True
                        )
                    assert not errors, errors
                    print(
                        "COLLECTION UI PASS: max 5; save/reload; reset; 60s policy; 1280/390; no page errors"
                    )
                finally:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
