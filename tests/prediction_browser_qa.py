"""Synthetic model/UI QA in a temporary ASGI app; no network or real data."""

import asyncio
import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from playwright.async_api import async_playwright
from browser_config import taobao_browser_options

URL = "https://prediction.test"
ROOT = Path(__file__).resolve().parent.parent


async def main():
    with tempfile.TemporaryDirectory(prefix="monitor-model-ui-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        (Path(temporary) / "public-access.json").write_text(json.dumps({"origin": URL}))
        (Path(temporary) / "access-token").write_text("synthetic-test-token")
        from backend import (
            app as server,
            domain,
            prediction,
            prediction_data as pd,
            store,
        )

        store.init()
        start = datetime(2024, 1, 1, 12, tzinfo=pd.TZ)
        at = start + timedelta(days=411)
        prediction.now = domain.utcnow = lambda: at
        products = []
        with store.connection() as db:
            for index, phase in enumerate((0, 30)):
                sku = str(101 + index)
                values = server.validated(
                    "product",
                    {
                        "name": f"合成测试显卡 {index+1}",
                        "url": f"https://item.taobao.com/item.htm?id={123+index}&skuId={sku}",
                        "model": "测试型号",
                        "spec": "8GB",
                        "paused": True,
                    },
                )
                # Create outside this transaction to avoid a nested SQLite write.
                products.append((values, phase, sku))
        for values, phase, sku in products:
            product = store.create("product", values)
            with store.connection() as db:
                for i in range(410):
                    price = round(
                        10000 + 1800 * math.sin((i + phase) * 2 * math.pi / 60)
                    )
                    extra = {
                        "source": "crawler",
                        "item_id": str(123 + phase // 30),
                        "requested_sku_id": sku,
                        "sku_verified": True,
                        "price_type": "regular",
                        "price_basis": "regular",
                        "product_context": {
                            "category": "显卡",
                            "brand": "合成测试品牌",
                            "model": "测试型号",
                            "spec": "8GB",
                            "shop_type": "普通店",
                        },
                    }
                    db.execute(
                        "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            f"{sku}-{i}",
                            product["id"],
                            (start + timedelta(days=i)).isoformat(),
                            price,
                            None,
                            "in_stock",
                            "success",
                            store.dumps(extra),
                            None,
                        ),
                    )
        outcome = prediction.train()
        assert outcome["state"] == "active", outcome
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app)
        ) as local:
            await browser_check(local)


async def browser_check(local):
    folder = ROOT / "output" / "prediction-qa"
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, **taobao_browser_options("chrome")
        )
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route_handler(route):
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

            await page.route("**/*", route_handler)
            await page.goto(URL + "/forecasts", wait_until="domcontentloaded")
            await page.get_by_label("访问口令", exact=True).fill("synthetic-test-token")
            await page.get_by_role("button", name="登录", exact=True).click()
            await page.get_by_role("heading", name="预测中心", exact=True).wait_for()
            await page.get_by_text("已验证 · 影子观察", exact=True).wait_for()
            state = await page.evaluate("fetch('/api/state').then(r=>r.json())")
            assert any(
                p["forecast"]["probability"] is not None or p["forecast"]["interval"]
                for p in state["products"]
            )
            await page.screenshot(
                path=str(folder / "synthetic-trained-desktop.png"), full_page=True
            )
            for width in (1280, 390):
                await page.set_viewport_size({"width": width, "height": 950})
                await page.wait_for_timeout(350)
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth + 2"
                )
            await page.screenshot(
                path=str(folder / "synthetic-trained-mobile.png"), full_page=True
            )
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.get_by_role("switch", name="影子模式", exact=True).click()
            await page.wait_for_function(
                "async () => {const r=await fetch('/api/state');const s=await r.json();return s.settings.shadow_mode===false && s.products.some(p=>p.forecast.model_status==='active')} "
            )
            state = await page.evaluate("fetch('/api/state').then(r=>r.json())")
            product = state["products"][0]
            await page.goto(
                URL + "/products/" + product["id"], wait_until="domcontentloaded"
            )
            await page.get_by_role(
                "heading", name=product["name"], exact=True
            ).wait_for()
            await page.get_by_role("tab", name="预测参考", exact=True).click()
            await page.get_by_text("预计观测最低价", exact=True).wait_for()
            await page.screenshot(
                path=str(folder / "synthetic-product-prediction.png"), full_page=True
            )
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "source": "synthetic temporary data",
                        "trained_ui": True,
                        "widths": [1440, 1280, 390],
                        "page_errors": errors,
                    }
                )
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
