"""Check one-click collection and its real eight-second wave boundary in isolation."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"


async def until(check, seconds=15):
    async with asyncio.timeout(seconds):
        while not check():
            await asyncio.sleep(0.02)


async def main():
    with tempfile.TemporaryDirectory(prefix="collect-all-qa-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        from backend import (
            app as server,
            access,
            collector,
            domain as d,
            store as s,
            worker,
        )

        s.init()
        access.rotate_read_only_token()
        for i in range(6):
            s.create(
                "product",
                server.validated(
                    "product",
                    {
                        "name": f"批量测试商品 {i+1}",
                        "url": f"https://item.taobao.com/item.htm?id={9000+i}&skuId={100+i}",
                        "paused": True,
                    },
                ),
            )
        s.create(
            "product",
            server.validated(
                "product", {"name": "未填链接的参考商品", "url": "", "paused": True}
            ),
        )
        s.set_settings({"circuit_failures": 100})
        release_first, release_last = asyncio.Event(), asyncio.Event()
        started, first_finished = [], []
        active = set()
        peak = 0

        async def collect(product):
            nonlocal peak
            started.append((product["id"], time.monotonic()))
            number = len(started)
            active.add(product["id"])
            peak = max(peak, len(active))
            try:
                if number <= 5:
                    await release_first.wait()
                    first_finished.append(time.monotonic())
                    sku = product["url"].split("skuId=")[1]
                    return {
                        "price": 12345,
                        "status": "success",
                        "stock": "in_stock",
                        "sku_verified": True,
                        "requested_sku_id": sku,
                        "selected_sku_id": sku,
                        "price_type": "regular",
                    }
                await release_last.wait()
                return {"price": None, "status": "price_masked", "stock": "unknown"}
            finally:
                active.remove(product["id"])

        collector.collect = collect
        pool = worker.CollectionPool()

        async def dispatch():
            while True:
                pool.dispatch()
                await asyncio.sleep(0.02)

        dispatcher = None
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
                        viewport={"width": 1280, "height": 832}
                    )
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    async def intercept(route):
                        request = route.request
                        if not request.url.startswith(URL + "/"):
                            await route.abort()
                            return
                        if request.url.endswith("/api/stream"):
                            event = {
                                "health": d.health(),
                                "session": collector.session_info(),
                            }
                            await route.fulfill(
                                status=200,
                                content_type="text/event-stream",
                                body="retry: 500\ndata: " + json.dumps(event) + "\n\n",
                            )
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
                    button = page.get_by_role("button", name="一键采集全部", exact=True)
                    await button.click()
                    await page.get_by_text(
                        "已排队 6 个，正在采集 0 个；跳过 1 个无链接商品、0 个无效链接",
                        exact=True,
                    ).wait_for()
                    await page.get_by_text(
                        "一键采集：已完成 0 / 6 个，正在采集 0 个，等待 6 个",
                        exact=True,
                    ).wait_for()
                    await page.locator('.sidebar a[href="/products"]').click()
                    await button.click()
                    await page.get_by_text(
                        "已有一键采集任务正在进行，本次未重复添加", exact=True
                    ).wait_for()
                    with s.connection() as db:
                        assert (
                            db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 6
                        )
                    await page.get_by_role("link", name="采集管理", exact=True).click()
                    output = ROOT / "output/collect-all-qa"
                    output.mkdir(parents=True, exist_ok=True)
                    for width in (1280, 390):
                        await page.set_viewport_size({"width": width, "height": 832})
                        await page.wait_for_timeout(250)
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth+2"
                        ), width
                        await page.screenshot(
                            path=str(output / f"collect-all-{width}.png"),
                            full_page=True,
                        )
                    await page.set_viewport_size({"width": 1280, "height": 832})
                    dispatcher = asyncio.create_task(dispatch())
                    await until(lambda: len(started) == 5)
                    assert peak == 5
                    release_first.set()
                    dialog = page.get_by_role("dialog", name="采集结果", exact=True)
                    await dialog.get_by_text(
                        "已完成 5 个商品：成功 5 个，失败 0 个", exact=True
                    ).wait_for()
                    await until(lambda: len(started) == 6)
                    gap = started[5][1] - max(first_finished)
                    assert gap >= 8, gap
                    await dialog.get_by_role(
                        "button", name="知道了", exact=True
                    ).click()
                    await dialog.wait_for(state="hidden")
                    release_last.set()
                    await dialog.get_by_text(
                        "已完成 1 个商品：成功 0 个，失败 1 个", exact=True
                    ).wait_for()
                    assert d.health()["queued"] == 0 and d.health()["batch"] is None
                    assert all(product["paused"] for product in s.entities("product"))
                    assert not errors, errors
                    report = {
                        "buttons_on_products_and_collection": True,
                        "queued": 6,
                        "skipped_no_url": 1,
                        "duplicates_prevented": True,
                        "peak_concurrency": peak,
                        "actual_wave_gap_seconds": round(gap, 3),
                        "success_and_failure_dialogs": True,
                        "automatic_pause_preserved": True,
                        "widths": [1280, 390],
                        "page_errors": errors,
                    }
                    (output / "report.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2)
                    )
                    print(json.dumps(report, ensure_ascii=False))
                finally:
                    if dispatcher:
                        dispatcher.cancel()
                        await asyncio.gather(dispatcher, return_exceptions=True)
                    await pool.close()
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
