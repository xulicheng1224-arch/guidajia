"""Real browser workflow with temporary data and simulated official gateways."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / "output" / "official-api-qa"


def serve(port):
    import uvicorn
    from backend import app as server, collector, official_api
    async def request(platform, params):
        await asyncio.sleep(.15)
        if platform == "taobao":
            return 200, {"tbk_item_info_get_response": {"results": {"n_tbk_item": [{"num_iid":"123","zk_final_price":"199.99","reserve_price":"299.99","title":"淘宝测试显卡"}]}}}
        return 200, {"jd_union_open_goods_promotiongoodsinfo_query_responce": {"result": json.dumps({"code":200,"data":[{"skuId":999,"unitPrice":"188.88","goodsName":"京东测试显卡"}]})}}
    async def forbidden(*_):
        raise AssertionError("API mode must not launch the browser collector")
    official_api.request_json=request
    collector.collect=forbidden
    uvicorn.run(server.app,host="127.0.0.1",port=port,access_log=False)


async def exercise(base):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel="msedge",headless=True)
        try:
            context=await browser.new_context(viewport={"width":1440,"height":1000},reduced_motion="reduce")
            page=await context.new_page()
            errors=[]
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.on("console",lambda message:errors.append(message.text) if message.type=="error" else None)
            await page.goto(base)
            await page.locator("#email").fill("admin@example.com")
            await page.locator("#password").fill("BrowserAdmin123")
            await page.get_by_role("button",name="登录",exact=True).last.click()
            await page.locator(".sidebar").wait_for()
            await page.goto(base+"/settings")
            await page.locator('.settings-nav').get_by_role('button',name='价格来源',exact=True).click()
            await page.wait_for_url('**/settings?tab=source')
            assert await page.get_by_role('switch').count()==0
            await page.locator("label.ant-radio-button-wrapper").filter(has_text="官方 API").click()
            await page.get_by_role('switch').first.click()
            await page.locator("label.ant-radio-button-wrapper").filter(has_text="淘宝爬虫").click()
            await page.get_by_role('button',name='保存价格来源',exact=True).click()
            await page.get_by_text('价格来源已保存，采集队列将按此来源执行',exact=True).last.wait_for()
            await page.get_by_role('button',name='放弃修改',exact=True).last.click()
            await page.locator("label.ant-radio-button-wrapper").filter(has_text="官方 API").click()
            for i in range(2):
                await page.get_by_role("switch").nth(i).click()
                await page.get_by_label("AppKey",exact=True).nth(i).fill("browser-fixture-key")
                await page.get_by_label("AppSecret",exact=True).nth(i).fill("browser-fixture-secret")
            await page.locator("label.ant-radio-button-wrapper").filter(has_text="淘宝爬虫").click()
            assert await page.get_by_role('switch').count()==0
            await page.locator("label.ant-radio-button-wrapper").filter(has_text="官方 API").click()
            assert await page.get_by_label('AppKey',exact=True).first.input_value()=='browser-fixture-key'
            assert await page.get_by_label('AppSecret',exact=True).first.input_value()=='browser-fixture-secret'
            await page.get_by_role("button",name="保存价格来源",exact=True).click()
            await page.get_by_text("价格来源已保存，采集队列将按此来源执行",exact=True).last.wait_for()
            await expect(page.get_by_label("AppSecret",exact=True).first).to_have_value("")
            await page.screenshot(path=OUTPUT/"source-settings.png",full_page=True)
            state=await (await context.request.get(base+"/api/state")).json()
            assert state["settings"]["price_source"]=="api"
            assert "browser-fixture-secret" not in json.dumps(state)
            await page.goto(base+"/collection")
            await page.get_by_role("button",name="一键 API 采集全部",exact=True).click()
            assert await page.get_by_role("button",name="恢复定时采集",exact=True).is_visible()
            await page.get_by_role("button",name="知道了",exact=True).wait_for(timeout=20000)
            await page.get_by_role("button",name="知道了",exact=True).click()
            async with asyncio.timeout(15):
                while True:
                    jobs=await (await context.request.get(base+"/api/jobs")).json()
                    if len([j for j in jobs if j["status"]=="success"])==2:break
                    await asyncio.sleep(.1)
            await page.screenshot(path=OUTPUT/"api-collection.png",full_page=True)
            state=await (await context.request.get(base+"/api/state")).json()
            assert state['settings']['global_paused'] is True
            assert {p["current_price"] for p in state["products"]}=={19999,18888}
            jd=next(p for p in state["products"] if "jd.com" in p["url"])
            await page.goto(base+"/products/"+jd["id"])
            await page.get_by_role("heading",name="京东测试显卡",exact=True).wait_for()
            await page.screenshot(path=OUTPUT/"jd-price-history.png",full_page=True)
            await page.get_by_text("京东 API",exact=True).wait_for()
            await page.screenshot(path=OUTPUT/"jd-price-history.png",full_page=True)
            await page.goto(base+"/settings?tab=account")
            assert await page.get_by_role("button",name="校验 Cookie",exact=True).is_disabled()
            await page.locator('.settings-nav').get_by_role('button',name='价格来源',exact=True).click()
            await page.wait_for_url('**/settings?tab=source')
            await page.locator("label.ant-radio-button-wrapper").filter(has_text="淘宝爬虫").click()
            assert await page.get_by_role('switch').count()==0
            async with page.expect_response(lambda response: response.url==base+'/api/price-source' and response.request.method=='PUT') as saved:
                await page.get_by_role("button",name="保存价格来源",exact=True).click()
            assert (await saved.value).status==200
            settings=await (await context.request.get(base+"/api/price-source")).json()
            assert settings["mode"]=="crawler" and settings["providers"]["jd"]["configured"]
            assert not errors,errors
            print(json.dumps({"settings_navigation":True,"conditional_api_visibility":True,"api_draft_preserved":True,"official_configuration_ui":True,"secret_not_echoed":True,"api_one_click":True,"jd_history":True,"cookie_check_paused":True,"switch_back":True,"browser_errors":len(errors)}))
        except BaseException:
            print(json.dumps({"browser_errors":errors,"page_text":(await page.locator('body').inner_text())[-3500:]},ensure_ascii=False))
            raise
        finally:await browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix="official-api-qa-") as folder:
        env={**os.environ,"MONITOR_DATA_DIR":folder}
        subprocess.run([sys.executable,"-c","from backend import accounts,store as s; from backend.models import Product; accounts.provision_admin('BrowserAdmin123'); s.init(); s.set_settings({'global_paused':True,'ml_auto_train':False,'request_gap':0}); s.create('product',Product(name='淘宝测试显卡',url='https://item.taobao.com/item.htm?id=123',paused=True,interval_hours=24).model_dump(mode='json')); s.create('product',Product(name='京东测试显卡',url='https://item.jd.com/999.html',paused=True,interval_hours=24).model_dump(mode='json'))"],cwd=ROOT,env=env,check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        base=f"http://127.0.0.1:{port}"
        with (Path(folder)/"server.log").open("wb") as log:
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),"--serve",str(port)],cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                async def run():
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    for _ in range(100):
                        try:
                            with opener.open(base+"/api/ping",timeout=1):break
                        except OSError:await asyncio.sleep(.1)
                    await exercise(base)
                asyncio.run(run())
            except BaseException:
                log.flush()
                print((Path(folder)/"server.log").read_text(errors="replace")[-2000:])
                raise
            finally:
                process.terminate();process.wait(timeout=20)


if __name__=="__main__":
    serve(int(sys.argv[2])) if len(sys.argv)>1 and sys.argv[1]=="--serve" else main()
