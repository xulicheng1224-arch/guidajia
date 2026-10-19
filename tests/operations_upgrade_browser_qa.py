"""Real UI against temporary accounts and deterministic, isolated workers."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / "output" / "operations-upgrade-qa"


def serve(port):
    import uvicorn
    from backend import app as server, collector, prediction, worker
    async def collect(product):
        await asyncio.sleep(0.2)
        return {"status": "success", "price": 12345, "stock": "in_stock"}
    def train():
        time.sleep(6)
        return {"state": "insufficient"}
    async def idle():
        await asyncio.Event().wait()
    async def login():
        collector.runtime().LOGIN.update(status="waiting", message="等待测试登录窗口", issue=None)
        try:
            await asyncio.Event().wait()
        finally:
            collector.runtime().LOGIN.update(status="idle", message="旧登录窗口已关闭")
    collector.collect = collect
    collector.login = login
    prediction.train = train
    worker.housekeeping = idle
    original = server.app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            task = asyncio.create_task(worker.run())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    server.app.router.lifespan_context = lifespan
    uvicorn.run(server.app, host="127.0.0.1", port=port, access_log=False)


async def exercise(base):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base)
            await page.locator("#email").fill("admin@example.com")
            await page.locator("#password").fill("BrowserAdmin123")
            await page.get_by_role("button", name="登录", exact=True).last.click()
            await page.locator(".sidebar").wait_for()
            await page.goto(base + "/collection")
            gap = page.get_by_label("同账号商品启动间隔（秒）", exact=True)
            await gap.wait_for()
            assert await gap.input_value() == "5"
            await gap.fill("7")
            await page.get_by_role("switch", name="显示采集浏览器窗口").click()
            await page.get_by_role("button", name="保存采集策略", exact=True).click()
            await page.get_by_text("采集策略已保存", exact=True).last.wait_for()
            await page.reload()
            await gap.wait_for()
            assert await gap.input_value() == "7"
            assert await page.get_by_role("switch", name="显示采集浏览器窗口").get_attribute("aria-checked") == "true"
            await gap.fill("5")
            await page.get_by_role("switch", name="显示采集浏览器窗口").click()
            await page.get_by_role("button", name="保存采集策略", exact=True).click()
            await page.get_by_text("采集策略已保存", exact=True).last.wait_for()
            await page.goto(base + "/settings?tab=account")
            await page.get_by_role("heading", name="淘宝 / 天猫账号", exact=True).wait_for()
            visible = await page.locator("body").inner_text()
            assert not any(term in visible for term in ("服务器 Mac", "MacBook", "MACBOOK EDITION", "为 macOS 设计", "本机运行", "/Users/"))
            await page.get_by_role("button", name="重新打开登录窗口", exact=True).click()
            await page.get_by_role("button", name="完成登录并保存", exact=True).wait_for()
            assert await page.get_by_role("button", name="上传 Cookie 文件", exact=True).is_enabled()
            await page.locator('input[type="file"]').set_input_files({"name": "latest-admin.json", "mimeType": "application/json",
                "buffer": json.dumps({"cookies": [{"name": "unb", "value": "fixture-new-upload", "domain": ".taobao.com"}]}).encode()})
            await page.get_by_text("淘宝 Cookie 已导入", exact=True).last.wait_for()
            assert (await (await context.request.get(base + "/api/state")).json())["session"]["status"] == "saved"
            assert await page.get_by_role("button", name="完成登录并保存", exact=True).count() == 0
            await page.get_by_role("button", name="校验 Cookie", exact=True).click()
            await page.get_by_text("已通过", exact=True).wait_for()
            await page.get_by_role("button", name="知道了", exact=True).wait_for()
            await page.get_by_role("button", name="知道了", exact=True).click()
            await page.screenshot(path=OUTPUT / "cookie-verified.png", full_page=True)
            created = await context.request.post(base + "/api/entities/product", data={"name": "排队任务", "url": "https://item.taobao.com/item.htm?id=124", "paused": False})
            assert created.status == 200
            await page.goto(base + "/collection")
            await page.get_by_text("定时采集已暂停，定时任务等待恢复；手动采集不受影响", exact=True).first.wait_for()
            await page.screenshot(path=OUTPUT / "queue-reason.png", full_page=True)
            await page.get_by_role("button", name="取消排队", exact=True).click()
            await page.get_by_text("排队任务已取消", exact=True).wait_for()

            normal = await browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            response = await normal.request.post(base + "/api/access/register", data={"email": "browser-queue@fake.example", "password": "BrowserUser123"})
            assert response.status == 200
            assert (await normal.request.post(base + '/api/session/import', data={'content':'unb=fixture-browser-user'})).status == 200
            user_page = await normal.new_page()
            user_page.on("pageerror", lambda error: errors.append(str(error)))
            await user_page.goto(base + '/settings?tab=account')
            await user_page.get_by_role('heading', name='淘宝 / 天猫账号', exact=True).wait_for()
            assert await user_page.get_by_role('button', name='打开登录窗口', exact=True).count() == 0
            assert await user_page.get_by_label('用于登录和采集的浏览器', exact=True).count() == 0
            visible = await user_page.locator('body').inner_text()
            assert not any(term in visible for term in ('服务器 Mac', 'MacBook', 'MACBOOK EDITION', '为 macOS 设计', '本机运行', '/Users/'))
            await user_page.get_by_role('button', name='校验 Cookie', exact=True).wait_for()
            assert await user_page.get_by_role('combobox', name='选择 Cookie 校验商品', exact=True).count() == 0
            await user_page.get_by_role('button', name='校验 Cookie', exact=True).click()
            await user_page.get_by_text('已通过', exact=True).wait_for()
            await user_page.get_by_role('button', name='知道了', exact=True).wait_for()
            await user_page.get_by_role('button', name='知道了', exact=True).click()
            await user_page.screenshot(path=OUTPUT / 'user-cookie-button.png', full_page=True)
            user_state = await (await normal.request.get(base + '/api/state')).json()
            assert user_state['products'] == []
            assert (await context.request.post(base + "/api/forecast-model/train", data={})).status == 200
            assert (await normal.request.post(base + "/api/forecast-model/train", data={})).status == 200
            await user_page.goto(base + "/forecasts")
            await user_page.get_by_text("等待训练名额，当前排队第 1 位", exact=True).wait_for()
            await user_page.screenshot(path=OUTPUT / "training-queue.png", full_page=True)
            await user_page.get_by_text("等待训练名额，当前排队第 1 位", exact=True).wait_for(state="hidden", timeout=20000)

            await page.goto(base + "/settings?tab=backup")
            await page.get_by_label("完整备份密码", exact=True).fill("BrowserBackup123!")
            await page.get_by_label("确认备份密码", exact=True).fill("BrowserBackup123!")
            await page.get_by_role("button", name="创建加密完整备份", exact=True).click()
            await page.get_by_role("button", name="校验备份", exact=True).wait_for(timeout=20000)
            await page.get_by_role("button", name="校验备份", exact=True).click()
            dialog = page.get_by_role("dialog")
            await dialog.get_by_label("校验备份密码", exact=True).fill("BrowserBackup123!")
            await dialog.get_by_role("button", name="解密并校验", exact=True).click()
            await dialog.wait_for(state="hidden")
            await page.screenshot(path=OUTPUT / "full-backup.png", full_page=True)
            await user_page.goto(base + "/settings?tab=backup")
            await user_page.get_by_role("button", name="立即备份", exact=True).wait_for()
            assert await user_page.get_by_role("button", name="创建加密完整备份", exact=True).count() == 0
            assert not errors, errors
            print(json.dumps({"server_device_details_hidden": True, "crawler_pacing_and_headed_controls": True, "upload_supersedes_waiting_login": True, "cookie_check": True, "normal_user_cookie_button_only": True, "queue_reason_and_cancel": True, "training_queue": True,
                              "encrypted_backup_and_check": True, "admin_only_backup": True, "browser_errors": 0}))
        finally:
            await browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="operations-upgrade-qa-") as folder:
        env = {**os.environ, "MONITOR_DATA_DIR": folder, "MONITOR_DISABLE_WORKER": "1"}
        subprocess.run([sys.executable, "-c", "from backend import accounts,collector,store as s; accounts.provision_admin('BrowserAdmin123'); s.init(); s.set_settings({'global_paused':True,'ml_auto_train':False,'request_gap':1}); s.create('product',{'name':'校验商品','url':'https://item.taobao.com/item.htm?id=123','paused':True,'interval_hours':24}); collector.session_state.save(collector.SESSION,{'cookies':[{'name':'unb','value':'fixture-only','domain':'.taobao.com'}],'origins':[]})"], cwd=ROOT, env=env, check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (Path(folder) / "server.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
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
                process.wait(timeout=20)


if __name__ == "__main__":
    serve(int(sys.argv[2])) if len(sys.argv) > 1 and sys.argv[1] == "--serve" else main()
