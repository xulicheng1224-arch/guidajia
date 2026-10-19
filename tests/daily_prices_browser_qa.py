"""Exercise daily price editing against a temporary app, never the live DB."""

import asyncio
import json
import os
import re
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"


async def main():
    with tempfile.TemporaryDirectory(prefix="monitor-daily-price-ui-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        from backend import (
            app as server,
            domain as d,
            prediction_data as pd,
            store as s,
        )

        s.init()
        p = s.create(
            "product",
            server.validated(
                "product",
                {
                    "name": "日价格编辑测试商品",
                    "url": "https://item.taobao.com/item.htm?id=123&skuId=101",
                    "paused": True,
                },
            ),
        )
        yesterday = (pd.day(d.utcnow()) - timedelta(days=1)).isoformat()
        empty_day = (pd.day(d.utcnow()) - timedelta(days=2)).isoformat()
        for hour, price in ((8, 10000), (12, 11000)):
            d.save_observation(
                p["id"],
                {
                    "price": price,
                    "status": "success",
                    "stock": "in_stock",
                    "sku_verified": True,
                },
                observed_at=f"{yesterday}T{hour:02d}:00:00+08:00",
            )
        errors = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
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

                    await page.route("**/*", route_handler)
                    await page.goto(
                        URL + "/products/" + p["id"], wait_until="domcontentloaded"
                    )
                    await page.get_by_role(
                        "heading", name=p["name"], exact=True
                    ).wait_for()
                    await page.get_by_role(
                        "tab", name="按天编辑价格", exact=True
                    ).click()
                    await page.get_by_role(
                        "button", name="编辑当日价格", exact=True
                    ).click()
                    dialog = page.get_by_role("dialog")
                    await dialog.get_by_label("当日价格（元）", exact=True).fill(
                        "88.88"
                    )
                    await dialog.get_by_label("修改原因", exact=True).fill(
                        "浏览器核对测试"
                    )
                    await dialog.locator(".ant-modal-footer .ant-btn-primary").click()
                    await dialog.wait_for(state="hidden")
                    await page.get_by_text("已按天编辑", exact=True).wait_for()
                    assert all(r["price"] == 8888 for r in d.records(p["id"]))
                    assert {r["raw_price"] for r in d.records(p["id"])} == {
                        10000,
                        11000,
                    }

                    await page.get_by_role(
                        "button", name="编辑当日价格", exact=True
                    ).click()
                    await dialog.get_by_label("当日价格（元）", exact=True).fill(
                        "99.99"
                    )
                    await dialog.get_by_label("修改原因", exact=True).fill("再次核对")
                    await dialog.locator(".ant-modal-footer .ant-btn-primary").click()
                    await dialog.wait_for(state="hidden")
                    await page.get_by_role("button", name="撤销", exact=True).click()
                    await page.locator(".ant-popconfirm .ant-btn-primary").click()
                    await page.get_by_text("已按天编辑", exact=True).wait_for(
                        state="hidden"
                    )
                    assert [r["price"] for r in d.records(p["id"])] == [10000, 11000]

                    await page.get_by_role(
                        "button", name="编辑 / 补录某一天", exact=True
                    ).click()
                    date_input = dialog.get_by_label("日期", exact=True)
                    await date_input.fill(empty_day)
                    await date_input.press("Enter")
                    await dialog.get_by_label("当日价格（元）", exact=True).fill(
                        "79.99"
                    )
                    await dialog.get_by_label("修改原因", exact=True).fill(
                        "补录历史订单"
                    )
                    await dialog.locator(".ant-modal-footer .ant-btn-primary").click()
                    await dialog.wait_for(state="hidden")
                    await page.get_by_text("已按天编辑", exact=True).wait_for()
                    virtual = [r for r in d.records(p["id"]) if r.get("virtual_daily")]
                    assert len(virtual) == 1 and virtual[0]["price"] == 7999
                    assert pd.day(virtual[0]["observed_at"]).isoformat() == empty_day

                    folder = ROOT / "output" / "daily-price-qa"
                    folder.mkdir(parents=True, exist_ok=True)
                    for width in (1280, 390):
                        await page.set_viewport_size({"width": width, "height": 900})
                        await page.wait_for_timeout(350)
                        await page.get_by_role(
                            "tab", name="按天编辑价格", exact=True
                        ).scroll_into_view_if_needed()
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 2"
                        )
                        await page.screenshot(
                            path=str(folder / f"daily-price-{width}.png"),
                            full_page=True,
                        )
                    await page.get_by_role(
                        "button", name="编辑当日价格", exact=True
                    ).first.click()
                    await dialog.wait_for()
                    await page.wait_for_timeout(350)
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 2"
                    )
                    await page.screenshot(
                        path=str(folder / "daily-price-mobile-editor.png"),
                        full_page=False,
                    )
                    assert not errors, errors
                    print(
                        json.dumps(
                            {
                                "daily_edit": True,
                                "update": True,
                                "undo": True,
                                "empty_day": True,
                                "widths": [1280, 390],
                                "page_errors": errors,
                            }
                        )
                    )
                finally:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
