"""Temporary-account UI review, including slow/failed request race regressions."""
import asyncio
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.request

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output" / "quiet-ui-qa"


async def exercise(base):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = await browser.new_context(viewport={"width":1440,"height":1000}, reduced_motion="reduce")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base)
            assert await page.locator("details.hint").count() == 1
            assert not await page.locator("details.hint").evaluate("e => e.open")
            await page.locator("#email").fill("admin@example.com")
            await page.locator("#password").fill("BrowserAdmin123")
            await page.get_by_role("button", name="登录", exact=True).last.click()
            await page.locator(".sidebar").wait_for()
            state = await (await context.request.get(base + "/api/state")).json()
            first, second = state["products"][:2]

            async def navigate(path):
                await page.evaluate("path => {history.pushState(null, '', path); dispatchEvent(new PopStateEvent('popstate'));}", path)

            routes = ["/", "/products", f"/products/{second['id']}", "/compare", "/alerts", "/calendar", "/collection", "/forecasts", "/audit"]
            routes += ["/settings?tab=" + tab for tab in ("source", "account", "views", "appearance", "charts", "notifications", "data", "io", "backup", "security")]
            layouts = []
            for theme in ("light", "dark"):
                if (await page.locator("html").get_attribute("data-theme")) != theme:
                    await page.get_by_role("button", name="切换主题", exact=True).click()
                    await expect(page.locator("html")).to_have_attribute("data-theme", theme)
                for width in (1440, 390):
                    await page.set_viewport_size({"width":width, "height":1000})
                    for path in routes:
                        await navigate(path)
                        await page.wait_for_timeout(180)
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (path, width, theme)
                        assert not await page.locator(".context-help[open]").count()
                        layouts.append({"path":path,"width":width,"theme":theme})
                    await navigate("/settings?tab=account")
                    await page.screenshot(path=OUTPUT / f"account-{theme}-{width}.png", full_page=True)
            await page.set_viewport_size({"width":1440,"height":1000})

            # An obsolete detail request must never replace the next product.
            started, release = asyncio.Event(), asyncio.Event()
            async def slow_detail(route):
                response = await route.fetch()
                started.set()
                await release.wait()
                try:
                    await route.fulfill(response=response)
                except Exception:
                    pass  # the old request may already be aborted by the hook
            pattern = base + "/api/product/" + first["id"]
            await page.route(pattern, slow_detail)
            await navigate("/products/" + first["id"])
            await asyncio.wait_for(started.wait(), 10)
            await navigate("/products/" + second["id"])
            await page.get_by_role("heading", name=second["name"], exact=True).wait_for()
            release.set()
            await page.wait_for_timeout(300)
            assert await page.get_by_role("heading", name=second["name"], exact=True).is_visible()
            assert not await page.get_by_role("heading", name=first["name"], exact=True).count()
            await page.unroute(pattern, slow_detail)

            # A network error is an error, not a false "product does not exist".
            await navigate("/products")
            async def failed_detail(route):
                await route.fulfill(status=503, json={"detail":"测试连接失败"})
            await page.route(pattern, failed_detail)
            await navigate("/products/" + first["id"])
            await page.get_by_text("商品加载失败", exact=True).wait_for()
            assert not await page.get_by_text("商品不存在或已移至回收站", exact=True).count()
            await page.unroute(pattern, failed_detail)
            await page.get_by_role("button", name=re.compile(r"重\s*试")).click()
            await page.get_by_role("heading", name=first["name"], exact=True).wait_for()

            # Two writes while the first /state refresh is held: the last write
            # must trigger another read instead of being silently dropped.
            await navigate("/settings?tab=data")
            state_started, state_release = asyncio.Event(), asyncio.Event()
            reads = 0
            async def delayed_state(route):
                nonlocal reads
                reads += 1
                response = await route.fetch()
                if reads == 1:
                    state_started.set()
                    await state_release.wait()
                await route.fulfill(response=response)
            await page.route(base + "/api/state", delayed_state)
            await page.get_by_role("button", name="切换主题", exact=True).click()
            await asyncio.wait_for(state_started.wait(), 10)
            field = page.get_by_label("新价连续几次一致才确认变价", exact=True)
            await field.fill("4")
            async with page.expect_response(lambda response: response.url == base + "/api/settings" and response.request.method == "PATCH"):
                await page.locator("#settings-data").get_by_role("button", name="保存设置", exact=True).click()
            state_release.set()
            await expect(page.locator("#settings-data").get_by_role("button", name="保存设置", exact=True)).not_to_have_class(re.compile("ant-btn-loading"))
            async with asyncio.timeout(10):
                while reads < 2:
                    await asyncio.sleep(.05)
            await page.wait_for_timeout(300)
            await page.get_by_role("button", name="放弃修改", exact=True).last.click()
            assert await field.input_value() == "4"
            await page.unroute(base + "/api/state", delayed_state)

            # A successful mutation followed by a failed refresh must not make
            # the user resubmit an already successful operation.
            async def failed_state(route):
                await route.fulfill(status=503, json={"detail":"测试状态刷新失败"})
            await page.route(base + "/api/state", failed_state)
            await field.fill("5")
            await page.locator("#settings-data").get_by_role("button", name="保存设置", exact=True).click()
            await page.get_by_text("操作已完成，但页面刷新失败，请稍后刷新页面。", exact=True).wait_for()
            saved = await (await context.request.get(base + "/api/state")).json()
            assert saved["settings"]["confirm_count"] == 5
            await page.unroute(base + "/api/state", failed_state)
            await page.locator(".settings-nav").get_by_role("button", name="外观与显示", exact=True).click()
            await page.get_by_role("heading", name="外观与显示", exact=True).wait_for()
            assert not await page.get_by_role("dialog").count()  # no false dirty form

            # Ordinary instructions stay collapsed; actual Cookie errors remain visible.
            await navigate("/settings?tab=data")
            help_box = page.locator(".context-help").filter(has_text="规格与价格口径")
            await help_box.locator("summary").click()
            assert await help_box.evaluate("element => element.open")
            assert await help_box.get_by_text("不同容量、版本、盒装和散片", exact=False).is_visible()
            await help_box.locator("summary").click()
            async def critical_state(route):
                response = await route.fetch()
                body = await response.json()
                body["session"]["verification"] = {"state":"verification_required","message":"测试：请完成淘宝验证"}
                await route.fulfill(response=response, json=body)
            await page.route(base + "/api/state", critical_state)
            await page.goto(base + "/settings?tab=account")
            await page.locator(".ant-alert").get_by_text("测试：请完成淘宝验证", exact=True).wait_for()
            await page.unroute(base + "/api/state", critical_state)

            # Non-secure HTTP browsers may not expose the Clipboard API.
            response = await context.request.post(base + "/api/security/full-backups", data={"password":"BrowserBackup123!","include_cookies":False})
            assert response.status == 200
            await page.goto(base + "/settings?tab=backup")
            await page.get_by_role("button", name="复制恢复命令", exact=True).wait_for()
            await page.evaluate("Object.defineProperty(navigator, 'clipboard', {value:undefined, configurable:true})")
            await page.get_by_role("button", name="复制恢复命令", exact=True).click()
            await page.get_by_role("dialog").locator(".ant-modal-confirm-title").wait_for()
            assert "restore_backup.py" in await page.get_by_role("dialog").inner_text()
            assert not errors, errors
            result = {"layouts_reviewed":len(layouts),"collapsed_help":True,"stale_detail_guard":True,"resource_failure_and_retry":True,"queued_refresh_after_write":True,"saved_write_with_failed_refresh":True,"clipboard_fallback":True,"critical_errors_visible":True,"help_expandable":True,"browser_errors":len(errors)}
            (OUTPUT / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps(result))
        except BaseException:
            await page.screenshot(path=OUTPUT / "failure.png", full_page=True)
            print(json.dumps({"url":page.url,"browser_errors":errors,"visible_text":(await page.locator("body").inner_text())[-2500:]},ensure_ascii=False))
            raise
        finally:
            await browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="quiet-ui-qa-") as folder:
        env = {**os.environ,"MONITOR_DATA_DIR":folder,"MONITOR_DISABLE_WORKER":"1"}
        setup = """
from datetime import timedelta
from backend import accounts, app as server, domain as d, store as s
accounts.provision_admin('BrowserAdmin123')
s.init()
s.set_settings({'global_paused':True,'ml_auto_train':False})
for i in range(2):
    p=s.create('product',server.validated('product',{'name':f'界面回归商品 {i+1}','url':f'https://item.taobao.com/item.htm?id={123+i}&skuId={101+i}','paused':True,'interval_hours':24}))
    for day in range(4):
        d.save_observation(p['id'],{'price':12345+i*100,'status':'success','stock':'in_stock','sku_verified':True,'selected_sku_id':str(101+i),'price_type':'regular'},observed_at=(d.utcnow()-timedelta(days=day)).isoformat())
"""
        subprocess.run([sys.executable,"-c",setup], cwd=ROOT,env=env,check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            port=sock.getsockname()[1]
        base=f"http://127.0.0.1:{port}"
        with (Path(folder)/"server.log").open("wb") as log:
            process=subprocess.Popen([sys.executable,"-m","uvicorn","backend.app:app","--host","127.0.0.1","--port",str(port),"--no-access-log"],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                async def run():
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    for _ in range(100):
                        try:
                            with opener.open(base+"/api/ping",timeout=1):break
                        except OSError:await asyncio.sleep(.1)
                    await exercise(base)
                asyncio.run(run())
            finally:
                process.terminate()
                process.wait(timeout=20)


if __name__ == "__main__":
    main()
