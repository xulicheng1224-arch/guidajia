"""Real UI against temporary data with persistently failing Cookie checks."""

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.request

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def serve(port):
    import uvicorn
    from backend import app as server, collector
    async def collect(_):
        await asyncio.sleep(.08)
        return {"status":"blocked", "price":None, "stock":"unknown"}
    collector.collect = collect
    uvicorn.run(server.app, host="127.0.0.1", port=port, access_log=False)


async def exercise(base):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = await browser.new_context(viewport={"width":1440,"height":1000}, reduced_motion="reduce")
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base)
            await page.locator("#email").fill("admin@example.com")
            await page.locator("#password").fill("BrowserAdmin123")
            await page.get_by_role("button", name="登录", exact=True).last.click()
            await page.locator(".sidebar").wait_for()
            async def state():
                return await (await context.request.get(base + "/api/state")).json()
            initial = await state()
            assert initial["session"]["status"] == "expired"
            until = initial["settings"]["circuit_until"]
            chosen = initial["products"][0]
            await page.goto(base + "/products/" + chosen["id"])
            await page.get_by_role("heading", name=chosen["name"], exact=True).wait_for()
            button = page.get_by_role("button", name="立即重抓", exact=True)
            assert await button.is_enabled()
            await button.click()
            await page.get_by_role("button", name="知道了", exact=True).wait_for(timeout=15000)
            await page.get_by_role("button", name="知道了", exact=True).click()
            await page.goto(base + "/collection")
            await page.get_by_role("button", name="一键采集全部", exact=True).click()
            async with asyncio.timeout(20):
                while True:
                    data = await state()
                    batch = data["health"].get("batch")
                    if batch and batch["completed"] == 5 and batch["waiting"] == 2:
                        assert data["health"]["queue"]["reason"] == "batch_cooldown"
                        break
                    await asyncio.sleep(.1)
                while (await state())["health"]["queued"]:
                    await asyncio.sleep(.1)
            final = await state()
            jobs = await (await context.request.get(base + "/api/jobs")).json()
            assert len(jobs) == 8 and all(row["status"] == "failed" and row["attempts"] == 1 for row in jobs)
            assert final["settings"]["global_paused"] is True
            assert final["settings"]["circuit_until"] == until
            assert all(product["paused"] for product in final["products"])
            assert not errors, errors
            print(json.dumps({"historical_cookie_failure_bypassed":True,"paused_product_manual_button_enabled":True,
                "all_eight_attempts_finished":True,"five_concurrent_eight_second_waves":True,
                "automatic_pause_and_circuit_preserved":True,"browser_errors":len(errors)}))
        finally:
            await browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="manual-priority-qa-") as folder:
        env = {**os.environ, "MONITOR_DATA_DIR":folder}
        env.pop("MONITOR_DISABLE_WORKER", None)
        setup = """
from datetime import timedelta
from backend import accounts, collector, domain as d, session_verification, store as s
from backend.models import Product
accounts.provision_admin('BrowserAdmin123')
s.init()
s.set_settings({'global_paused':True,'ml_auto_train':False,'crawler_start_gap':0,'request_gap':0,'max_retries':0,'circuit_until':(d.utcnow()+timedelta(hours=1)).isoformat()})
for i in range(7):
    s.create('product',Product(name=f'人工优先测试 {i+1}',url=f'https://item.taobao.com/item.htm?id={800+i}',paused=True).model_dump(mode='json'))
collector.accept_session({'cookies':[{'name':'unb','value':'fixture-only','domain':'.taobao.com'}],'origins':[]},'fixture')
session_verification.write(collector.SESSION,{'state':'verification_required','message':'fixture verification failure'})
"""
        subprocess.run([sys.executable,"-c",setup], cwd=ROOT, env=env, check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        with (Path(folder) / "server.log").open("wb") as log:
            process = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--serve",str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                async def run():
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    for _ in range(100):
                        try:
                            with opener.open(base + "/api/ping", timeout=1):
                                break
                        except OSError:
                            await asyncio.sleep(.1)
                    await exercise(base)
                asyncio.run(run())
            finally:
                process.terminate()
                process.wait(timeout=20)


if __name__ == "__main__":
    serve(int(sys.argv[2])) if len(sys.argv)>1 and sys.argv[1]=="--serve" else main()
