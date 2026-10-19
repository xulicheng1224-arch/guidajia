"""Verify the actual supplied price dataset in an isolated app before importing it."""

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


async def main():
    dataset = json.loads((ROOT / "output/history-import/normalized.json").read_text())
    output = ROOT / "output/history-import/qa"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reference-history-qa-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        from backend import app as server, access, domain as d, prediction, store as s

        s.init()
        access.rotate_read_only_token()
        errors = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        ) as local:
            await local.get(URL + "/api/bootstrap")
            products = []
            for series in dataset["products"]:
                p = (
                    (
                        await local.post(
                            URL + "/api/entities/product", json=series["product"]
                        )
                    )
                    .raise_for_status()
                    .json()
                )
                values = [{**row, "product_id": p["id"]} for row in series["rows"]]
                response = (
                    (
                        await local.post(
                            URL + "/api/import/observations",
                            json={"text": json.dumps(values), "preview": False},
                        )
                    )
                    .raise_for_status()
                    .json()
                )
                assert response["new"] == 31, response
                products.append(p)
            result = prediction.train()
            assert result["state"] == "insufficient", result
            report = prediction.report()
            assert report["progress"]["daily_points"] == 155, report
            assert report["reference_data"] == {
                "points": 155,
                "measured": 35,
                "carried_forward": 120,
            }, report
            assert sum(len(d.records(p["id"])) for p in products) == 155
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True, **taobao_browser_options("chrome")
                )
                try:
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 900}
                    )
                    page = await context.new_page()
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
                    paths = {
                        "dashboard": "/",
                        "products": "/products",
                        "detail": "/products/" + products[0]["id"],
                        "forecasts": "/forecasts",
                    }
                    for name, path in paths.items():
                        await page.goto(URL + path, wait_until="domcontentloaded")
                        await page.locator(".page-heading h1").wait_for()
                        await page.wait_for_timeout(650)
                        for width in (1280, 390):
                            await page.set_viewport_size(
                                {"width": width, "height": 900}
                            )
                            await page.wait_for_timeout(250)
                            assert await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth + 2"
                            ), (name, width)
                            await page.screenshot(
                                path=str(output / f"{name}-{width}.png"), full_page=True
                            )
                        await page.set_viewport_size({"width": 1280, "height": 900})
                    await page.goto(
                        URL + "/products/" + products[0]["id"],
                        wait_until="domcontentloaded",
                    )
                    await page.get_by_role(
                        "heading", name=products[0]["name"], exact=True
                    ).wait_for()
                    assert await page.get_by_role(
                        "button", name="恢复", exact=True
                    ).is_disabled()
                    await page.get_by_role(
                        "tab", name="按天编辑价格", exact=True
                    ).click()
                    await page.get_by_text(
                        "参考价格独立保存，不绑定淘宝 SKU", exact=True
                    ).wait_for()
                    await page.get_by_text("沿用前值", exact=True).first.wait_for()
                    await page.screenshot(
                        path=str(output / "daily-reference.png"), full_page=True
                    )
                    await page.get_by_role("button", name="编辑", exact=True).first.click()
                    drawer = page.get_by_role("dialog")
                    await drawer.get_by_label("商品链接", exact=True).fill(
                        "https://item.taobao.com/item.htm?id=123&skuId=456"
                    )
                    await drawer.get_by_role(
                        "button", name="保存商品", exact=True
                    ).click()
                    await drawer.wait_for(state="hidden")
                    updated = s.get(products[0]["id"], "product")
                    assert updated["url"].endswith("skuId=456") and updated["paused"]
                    assert len(d.records(updated["id"])) == 31
                    assert prediction.report()["reference_data"]["points"] == 155
                    # Read-only mode uses the same reference history without edits.
                    viewer = await browser.new_context(
                        viewport={"width": 1280, "height": 900},
                        extra_http_headers={
                            "Authorization": "Bearer " + access.read_only_token()
                        },
                    )
                    viewer_page = await viewer.new_page()
                    viewer_page.on("pageerror", lambda e: errors.append(str(e)))
                    await viewer_page.route("**/*", intercept)
                    await viewer_page.goto(
                        URL + "/products/" + products[1]["id"],
                        wait_until="domcontentloaded",
                    )
                    await viewer_page.get_by_text("只读访问", exact=True).wait_for()
                    await viewer_page.get_by_role(
                        "tab", name="每日价格", exact=True
                    ).click()
                    await viewer_page.get_by_text(
                        "北京时间 · 独立参考价格", exact=True
                    ).wait_for()
                    assert (
                        await viewer_page.get_by_role(
                            "button", name="编辑当日价格", exact=True
                        ).count()
                        == 0
                    )
                    await viewer_page.get_by_text(
                        "沿用前值", exact=True
                    ).first.wait_for()
                    await viewer_page.screenshot(
                        path=str(output / "viewer-reference.png"), full_page=True
                    )
                    assert not errors, errors
                    print(
                        json.dumps(
                            {
                                "products": 5,
                                "prices": 155,
                                "measured": 35,
                                "carried_forward": 120,
                                "trained_daily_points": 155,
                                "mature_samples": report["progress"]["samples"],
                                "link_completion_preserves_history": True,
                                "viewer_reference_readable": True,
                                "page_errors": errors,
                            },
                            ensure_ascii=False,
                        )
                    )
                finally:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
