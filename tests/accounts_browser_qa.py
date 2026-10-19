"""Real-browser account flows against an isolated temporary server/database."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output" / "accounts-qa"


async def exercise(base):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base)
            await page.locator("#email").wait_for()
            await page.screenshot(path=OUTPUT / "login-light.png", full_page=True)
            await page.get_by_role("button", name="注册", exact=True).click()
            await page.locator("#email").fill("browser-user@notreal.example")
            await page.locator("#password").fill("BrowserUser123")
            await page.locator("#confirm-password").fill("BrowserUser123")
            await page.get_by_role("button", name="注册并进入").click()
            await page.locator(".sidebar").wait_for()
            state = await (await context.request.get(base + "/api/state")).json()
            assert state["products"] == [] and state["access"]["role"] == "user"
            await page.goto(base + "/settings?tab=account")
            await page.get_by_role("button", name="上传 Cookie 文件").wait_for()
            assert await page.get_by_role("button", name="打开登录窗口", exact=True).count() == 0
            await page.goto(base + "/settings?tab=security")
            await page.get_by_role("heading", name="账号与安全").wait_for()
            assert await page.get_by_text("注册账号管理", exact=True).count() == 0
            await page.screenshot(path=OUTPUT / "user-security.png", full_page=True)

            admin = await browser.new_context(viewport={"width": 1440, "height": 900})
            admin_page = await admin.new_page()
            admin_page.on("pageerror", lambda error: errors.append(str(error)))
            await admin_page.goto(base)
            await admin_page.locator("#email").fill("admin@example.com")
            await admin_page.locator("#password").fill("BrowserAdmin123")
            await admin_page.get_by_role("button", name="登录", exact=True).last.click()
            await admin_page.locator(".sidebar").wait_for()
            own = await (await admin.request.get(base + "/api/state")).json()
            assert len(own["products"]) == 1
            await admin_page.goto(base + "/settings?tab=security")
            await admin_page.get_by_role("button", name="重置密码", exact=True).wait_for()
            await admin_page.screenshot(path=OUTPUT / "admin-security.png", full_page=True)
            await admin_page.get_by_role("button", name="重置密码", exact=True).click()
            dialog = admin_page.get_by_role("dialog")
            await dialog.get_by_label("新密码", exact=True).fill("ResetBrowser123")
            await dialog.get_by_label("确认密码", exact=True).fill("ResetBrowser123")
            await dialog.get_by_role("button", name="重置并退出该账号的登录").click()
            await dialog.wait_for(state="hidden")
            await page.reload()
            await page.locator("#email").wait_for()
            await page.locator("#email").fill("browser-user@notreal.example")
            await page.locator("#password").fill("ResetBrowser123")
            await page.get_by_role("button", name="登录", exact=True).last.click()
            await page.locator(".sidebar").wait_for()
            await page.goto(base + "/settings?tab=security")
            await page.get_by_label("当前密码", exact=True).fill("ResetBrowser123")
            await page.get_by_label("新密码", exact=True).fill("ChangedBrowser123")
            await page.get_by_label("确认新密码", exact=True).fill("ChangedBrowser123")
            await page.get_by_role("button", name="修改密码并重新登录").click()
            await page.locator("#email").wait_for()
            await page.emulate_media(color_scheme="dark")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_role("button", name="注册", exact=True).click()
            await page.screenshot(path=OUTPUT / "register-mobile-dark.png", full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors, errors
            print(json.dumps({"registration": True, "admin_login": True, "isolated_data": True,
                              "reset_revokes_login": True, "password_change": True, "mobile_overflow": False,
                              "browser_errors": len(errors)}))
        finally:
            await browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="accounts-browser-qa-") as folder:
        env = {**os.environ, "MONITOR_DATA_DIR": folder, "MONITOR_DISABLE_WORKER": "1"}
        subprocess.run([sys.executable, "-c", "from backend import accounts,store as s; accounts.provision_admin('BrowserAdmin123'); s.init(); s.create('product',{'name':'管理员原有商品','url':'https://item.taobao.com/item.htm?id=123','paused':True,'interval_hours':24})"], cwd=ROOT, env=env, check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (Path(folder) / "server.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                async def run():
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    for _ in range(100):
                        try:
                            with opener.open(base + "/api/ping", timeout=1):
                                break
                        except OSError:
                            await asyncio.sleep(0.1)
                    await exercise(base)
                asyncio.run(run())
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    main()
