"""Exercise completion dialogs in an isolated app, without live crawling."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"


async def wait_until(check):
    async with asyncio.timeout(10):
        while not check():
            await asyncio.sleep(0.05)


async def main():
    with tempfile.TemporaryDirectory(prefix="collection-results-qa-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        from backend import app as server, access, store as s

        s.init()
        access.rotate_read_only_token()
        products = [
            s.create(
                "product",
                server.validated(
                    "product",
                    {
                        "name": name,
                        "url": f"https://item.taobao.com/item.htm?id={8000+i}",
                        "paused": True,
                    },
                ),
            )
            for i, name in enumerate(("测试成功显卡", "测试超时内存", "测试验证页主板"))
        ]

        def finish(index, code="success"):
            p = products[index]
            job = s.enqueue(p["id"])
            success = code == "success"
            result = {
                "price": 12345 if success else None,
                "status": code,
                "diagnostics": (
                    {
                        "stage": "subprocess",
                        "timeout_seconds": 60,
                        "exception_message": "采集超过 60 秒，已终止爬虫及浏览器子进程；本次不自动重试",
                    }
                    if code == "timeout"
                    else {}
                ),
            }
            with s.connection() as db:
                db.execute(
                    "UPDATE jobs SET status=?,finished_at=?,attempts=1,error=?,result=? WHERE id=?",
                    (
                        "success" if success else "failed",
                        s.now(),
                        None if success else code,
                        s.dumps(result),
                        job,
                    ),
                )

        finish(0)  # Old history must not produce a completion dialog.
        errors, feed_requests = [], []
        offline = False
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        ) as local:
            await local.get(URL + "/api/bootstrap")
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True, **taobao_browser_options("chrome")
                )
                try:
                    page = await browser.new_page(
                        viewport={"width": 1280, "height": 900}
                    )
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    async def intercept(route):
                        request = route.request
                        if not request.url.startswith(
                            URL + "/"
                        ) or request.url.endswith("/api/stream"):
                            await route.abort()
                            return
                        if "/api/collection/results" in request.url:
                            feed_requests.append(request.url)
                            if offline:
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
                    await page.goto(URL + "/", wait_until="domcontentloaded")
                    await page.locator(".page-heading h1").wait_for()
                    await wait_until(lambda: len(feed_requests) >= 2)
                    dialog = page.get_by_role("dialog", name="采集结果", exact=True)
                    assert await dialog.count() == 0
                    await page.get_by_role(
                        "link", name="商品库", exact=False
                    ).first.click()
                    await page.get_by_role(
                        "heading", name="商品库", exact=True
                    ).wait_for()
                    finish(0)
                    await dialog.wait_for()
                    await dialog.get_by_text("采集价 ¥ 123.45", exact=True).wait_for()
                    finish(1, "timeout")
                    await dialog.get_by_text(
                        "已完成 2 个商品：成功 1 个，失败 1 个", exact=True
                    ).wait_for()
                    timeout_item = dialog.locator("article").filter(
                        has_text="测试超时内存"
                    )
                    await timeout_item.get_by_text("查看诊断详情", exact=True).click()
                    await timeout_item.get_by_text("采集时限", exact=True).wait_for()
                    await timeout_item.get_by_text(
                        "60 秒 · 超时终止，不自动重试", exact=True
                    ).wait_for()
                    await page.reload(wait_until="domcontentloaded")
                    await dialog.get_by_text(
                        "已完成 2 个商品：成功 1 个，失败 1 个", exact=True
                    ).wait_for()
                    s.set_settings({"theme": "dark"})
                    await page.reload(wait_until="domcontentloaded")
                    await dialog.wait_for()
                    await page.wait_for_function(
                        "document.documentElement.dataset.theme === 'dark'"
                    )
                    output = ROOT / "output/collection-results-qa"
                    output.mkdir(parents=True, exist_ok=True)
                    for width in (1280, 390):
                        await page.set_viewport_size({"width": width, "height": 900})
                        await page.wait_for_timeout(350)
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 2"
                        ), width
                        await page.screenshot(
                            path=str(output / f"results-dark-{width}.png"),
                            full_page=True,
                        )
                    await dialog.get_by_role(
                        "button", name="知道了", exact=True
                    ).click()
                    await dialog.wait_for(state="hidden")
                    await page.reload(wait_until="domcontentloaded")
                    previous = len(feed_requests)
                    await wait_until(lambda: len(feed_requests) >= previous + 2)
                    assert await dialog.count() == 0
                    offline = True
                    previous = len(feed_requests)
                    finish(2, "blocked")
                    await wait_until(lambda: len(feed_requests) > previous)
                    offline = False
                    await page.evaluate("window.dispatchEvent(new Event('focus'))")
                    await dialog.get_by_text(
                        "价格 None · 遇到验证页", exact=True
                    ).wait_for()
                    assert await dialog.locator("article").count() == 1
                    await dialog.get_by_role(
                        "button", name="知道了", exact=True
                    ).click()
                    await dialog.wait_for(state="hidden")
                    finish(0)
                    await dialog.get_by_text(
                        "已完成 1 个商品：成功 1 个，失败 0 个", exact=True
                    ).wait_for()
                    await dialog.get_by_role(
                        "button", name="查看采集管理", exact=True
                    ).click()
                    await page.get_by_role(
                        "heading", name="采集管理", exact=True
                    ).wait_for()
                    await dialog.wait_for(state="hidden")

                    reader_context = await browser.new_context(
                        extra_http_headers={
                            "Authorization": "Bearer " + access.read_only_token()
                        }
                    )
                    reader_page = await reader_context.new_page()
                    reader_page.on("pageerror", lambda error: errors.append(str(error)))
                    await reader_page.route("**/*", intercept)
                    async with reader_page.expect_response(
                        lambda response: "/api/collection/results" in response.url
                    ):
                        await reader_page.goto(URL + "/", wait_until="domcontentloaded")
                    finish(1, "timeout")
                    reader_dialog = reader_page.get_by_role(
                        "dialog", name="采集结果", exact=True
                    )
                    await reader_dialog.wait_for()
                    assert (
                        await reader_dialog.get_by_role(
                            "button", name="查看采集管理", exact=True
                        ).count()
                        == 0
                    )
                    await reader_dialog.get_by_role(
                        "button", name="知道了", exact=True
                    ).click()
                    assert not errors, errors
                    print(
                        json.dumps(
                            {
                                "success_dialog": True,
                                "failure_dialog": True,
                                "parallel_results_merged": True,
                                "diagnostics": True,
                                "old_history_skipped": True,
                                "unread_survives_reload": True,
                                "acknowledged_not_repeated": True,
                                "offline_catchup": True,
                                "reader_supported": True,
                                "widths": [1280, 390],
                                "page_errors": errors,
                            }
                        )
                    )
                finally:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
