"""Compare visible content and layouts before/after styling, in a temp app."""

import argparse
import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"
AT = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
FOLDER = ROOT / "output" / "design-qa"


async def main(mode):
    FOLDER.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="monitor-design-ui-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        from backend import app as server, domain as d, prediction, store as s

        s.init()
        d.utcnow = prediction.now = lambda: AT
        s.now = lambda: AT.isoformat()
        for i, name in enumerate(("5060 8G", "5060ti华硕天选", "1060")):
            p = s.create(
                "product",
                server.validated(
                    "product",
                    {
                        "name": name,
                        "url": f"https://item.taobao.com/item.htm?id={123+i}&skuId={101+i}",
                        "category": "显卡",
                        "brand": "测试品牌",
                        "model": "RTX 5060" if i == 0 else "RTX 5060 Ti",
                        "spec": "8GB" if i < 2 else "16GB",
                        "shop": "测试店铺",
                        "paused": True,
                    },
                ),
                entity_id=f"design-product-{i}",
            )
            for day in range(38):
                d.save_observation(
                    p["id"],
                    {
                        "price": (520000, 559900, 639900)[i] + (day % 5) * 1000,
                        "status": "success",
                        "stock": "in_stock",
                        "sku_verified": True,
                        "selected_sku_id": str(101 + i),
                        "price_type": "regular",
                    },
                    observed_at=(AT - timedelta(days=38 - day)).isoformat(),
                )
        s.create(
            "event",
            server.validated(
                "event",
                {
                    "name": "双十一",
                    "start": "2026-11-01",
                    "end": "2026-11-11",
                    "confidence": "官宣",
                },
            ),
            entity_id="design-event",
        )
        snapshots = {}
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
                        viewport={"width": 1440, "height": 1000}
                    )
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.add_init_script(f"""(() => {{
                        const OriginalDate=Date; const fixed={int(AT.timestamp()*1000)};
                        window.Date=class extends OriginalDate {{ constructor(...args) {{ if(args.length) super(...args); else super(fixed); }} static now() {{ return fixed; }} }};
                    }})();""")

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
                    paths = {
                        "dashboard": "/",
                        "products": "/products",
                        "detail": "/products/design-product-0",
                        "forecasts": "/forecasts",
                        "calendar": "/calendar",
                        "collection": "/collection",
                        "settings": "/settings?tab=security",
                    }
                    for name, path in paths.items():
                        await page.goto(URL + path, wait_until="domcontentloaded")
                        await page.locator(".page-heading h1").wait_for()
                        if name == "detail":
                            await page.get_by_role(
                                "tab", name="按天编辑价格", exact=True
                            ).click()
                        await page.wait_for_timeout(700)
                        snapshots[name] = " ".join(
                            (await page.locator(".page-content").inner_text()).split()
                        )
                        for width in (1440, 1280, 390):
                            await page.set_viewport_size(
                                {"width": width, "height": 1000}
                            )
                            await page.wait_for_timeout(300)
                            assert await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth + 2"
                            ), (name, width)
                            if width in (1440, 390):
                                await page.screenshot(
                                    path=str(FOLDER / f"{mode}-{name}-{width}.png"),
                                    full_page=True,
                                )
                        await page.set_viewport_size({"width": 1440, "height": 1000})
                    if mode == "verify":
                        baseline = json.loads(
                            (FOLDER / "content-baseline.json").read_text()
                        )
                        assert baseline == snapshots, {
                            k: {"before": baseline.get(k), "after": v}
                            for k, v in snapshots.items()
                            if baseline.get(k) != v
                        }
                        # Theme and reduced motion are visual state only. Do not
                        # write theme preferences into the real application's DB.
                        await page.goto(
                            URL + "/products/design-product-0",
                            wait_until="domcontentloaded",
                        )
                        await page.locator(".page-heading h1").wait_for()
                        await page.get_by_role(
                            "button", name="切换主题", exact=True
                        ).click()
                        await page.wait_for_function(
                            "document.documentElement.dataset.theme === 'dark'"
                        )
                        await page.wait_for_timeout(600)
                        contrast = await page.evaluate("""() => {
                          const luminance = value => {
                            const rgb = (value.match(/[\\d.]+/g) || []).slice(0,3).map(Number).map(v => {v /= 255; return v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4;});
                            return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;
                          };
                          const ratio = (a,b) => { const x=luminance(a), y=luminance(b); return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); };
                          const heading = getComputedStyle(document.querySelector('.page-heading h1'));
                          const cell = getComputedStyle(document.querySelector('.ant-table-tbody td'));
                          const table = getComputedStyle(document.querySelector('.ant-table'));
                          const bg=getComputedStyle(document.body).backgroundColor;
                          return {heading:ratio(heading.color,bg),table:ratio(cell.color,table.backgroundColor)};
                        }""")
                        assert (
                            contrast["heading"] >= 4.5 and contrast["table"] >= 4.5
                        ), contrast
                        await page.screenshot(
                            path=str(FOLDER / "verify-detail-dark.png"), full_page=True
                        )
                        await page.goto(
                            URL + "/settings?tab=io", wait_until="domcontentloaded"
                        )
                        await page.get_by_role("row").filter(
                            has_text="商品清单"
                        ).get_by_role("button", name="导入", exact=True).click()
                        dialog = page.get_by_role("dialog")
                        await dialog.get_by_role(
                            "button", name="下载 CSV 模板", exact=True
                        ).wait_for()
                        await page.wait_for_timeout(450)
                        await page.screenshot(
                            path=str(FOLDER / "verify-import-dark.png"), full_page=True
                        )
                        csv_text = "url,name,msrp\nhttps://item.taobao.com/item.htm?id=777&skuId=888,CSV模板验证,199900"
                        await dialog.locator("textarea").fill(csv_text)
                        await dialog.get_by_role(
                            "button", name="预览导入", exact=True
                        ).click()
                        await dialog.get_by_text("新增 1", exact=True).wait_for()
                        assert len(s.entities("product")) == 3
                        await dialog.get_by_role(
                            "button", name="确认导入", exact=True
                        ).click()
                        await dialog.wait_for(state="hidden")
                        imported = [
                            p
                            for p in s.entities("product")
                            if p["name"] == "CSV模板验证"
                        ]
                        assert (
                            len(imported) == 1
                            and imported[0]["paused"]
                            and imported[0]["msrp"] == 199900
                        )
                        await page.emulate_media(reduced_motion="reduce")
                        await page.goto(URL + "/", wait_until="domcontentloaded")
                        await page.locator(".page-heading h1").wait_for()
                        assert (
                            await page.locator(".page-scene").evaluate(
                                "el=>getComputedStyle(el).animationName"
                            )
                            == "none"
                        )
                    else:
                        (FOLDER / "content-baseline.json").write_text(
                            json.dumps(snapshots, ensure_ascii=False, indent=2)
                        )
                    assert not errors, errors
                    print(
                        json.dumps(
                            {
                                "mode": mode,
                                "pages": list(paths),
                                "widths": [1440, 1280, 390],
                                "visible_content_unchanged": mode == "verify",
                                "page_errors": errors,
                            }
                        )
                    )
                finally:
                    await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["baseline", "verify"])
    asyncio.run(main(parser.parse_args().mode))
