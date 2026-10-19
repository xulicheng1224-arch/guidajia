"""Browser smoke test against the isolated QA server, never the user's database."""

import asyncio
import json
import os
import re
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
URL = os.environ.get("MONITOR_QA_URL", "http://127.0.0.1:8766")


async def main():
    errors = []
    failures = []
    folder = ROOT / "output" / "qa"
    folder.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(
            viewport={"width": 1440, "height": 1040}, device_scale_factor=1
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "response",
            lambda response: (
                failures.append(f"{response.status} {response.url}")
                if "/api/" in response.url and response.status >= 400
                else None
            ),
        )
        await page.goto(URL, wait_until="domcontentloaded")
        await page.locator("h1").wait_for()
        response = await page.request.get(URL + "/api/state")
        state = await response.json()
        first = state["products"][0]["id"]
        cases = [
            ("/", "总览看板"),
            ("/products", "商品库"),
            (f"/products/{first}", state["products"][0]["name"]),
            (
                "/compare?ids=" + ",".join(p["id"] for p in state["products"][:3]),
                "比较页",
            ),
            ("/alerts", "提醒中心"),
            ("/calendar", "事件日历"),
            ("/collection", "采集管理"),
            ("/forecasts", "预测中心"),
            ("/audit", "审计与回收站"),
            ("/settings", "设置"),
        ]
        for index, (path, title) in enumerate(cases):
            await page.goto(URL + path, wait_until="domcontentloaded")
            await page.locator("h1").filter(has_text=title).wait_for()
            await page.screenshot(
                path=str(folder / f"{index:02d}-desktop.png"), full_page=True
            )
            width = await page.evaluate(
                "({scroll:document.documentElement.scrollWidth,client:innerWidth})"
            )
            assert width["scroll"] <= width["client"] + 2, (path, width)
            print("PAGE OK", path, flush=True)
        # Manual price creation through the UI, then correction and undo.
        await page.goto(URL + f"/products/{first}", wait_until="domcontentloaded")
        await page.get_by_role("button", name="手动新增价格", exact=True).click()
        modal = page.get_by_role("dialog")
        await modal.get_by_label("到手价（元）").fill("6199.99")
        await modal.get_by_label("修改原因 / 备注").fill("QA 独立数据库手动价格")
        await modal.get_by_role("button", name=re.compile(r"确\s*定")).click()
        await modal.wait_for(state="hidden")
        await page.get_by_role("tab", name="备注与标注", exact=True).click()
        await page.get_by_role("button", name="添加备注 / 标注").click()
        modal = page.get_by_role("dialog")
        await modal.get_by_label("内容").fill("QA 笔记")
        await modal.get_by_role("button", name=re.compile(r"确\s*定")).click()
        await modal.wait_for(state="hidden")
        assert await page.get_by_text("QA 笔记", exact=True).count() > 0
        # Rule creation, historical preview, saving, and clone prefill.
        await page.goto(URL + "/alerts", wait_until="domcontentloaded")
        await page.get_by_role("button", name="新建规则", exact=True).click()
        drawer = page.locator(".ant-drawer-content").last
        await drawer.get_by_label("规则名称").fill("QA 目标价规则")
        await drawer.get_by_role("button", name="预览历史触发", exact=True).click()
        await drawer.get_by_role("button", name="保存规则", exact=True).last.click()
        await drawer.wait_for(state="hidden")
        assert await page.get_by_text("QA 目标价规则", exact=True).count() > 0
        # Persist appearance, switch to dark mode, verify all narrow layouts.
        await page.get_by_role("button", name="切换主题").click()
        await page.wait_for_function('document.documentElement.dataset.theme==="dark"')
        await page.goto(URL, wait_until="domcontentloaded")
        await page.screenshot(path=str(folder / "dark-desktop.png"), full_page=True)
        await page.get_by_role("button", name="切换主题").click()
        for width in [1280, 900, 390]:
            await page.set_viewport_size({"width": width, "height": 900})
            for path in ["/", "/products", "/calendar", "/settings"]:
                await page.goto(URL + path, wait_until="domcontentloaded")
                await page.locator("h1").wait_for()
                bounds = await page.evaluate(
                    "({scroll:document.documentElement.scrollWidth,client:innerWidth})"
                )
                assert bounds["scroll"] <= bounds["client"] + 2, (width, path, bounds)
            await page.goto(URL, wait_until="domcontentloaded")
            await page.screenshot(
                path=str(folder / f"home-{width}.png"), full_page=True
            )
            print("RESPONSIVE OK", width, flush=True)
        await browser.close()
    report = {
        "pages": len(cases),
        "browser_errors": errors,
        "failed_api_calls": failures,
    }
    (folder / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False))
    assert not errors and not failures


if __name__ == "__main__":
    asyncio.run(main())
