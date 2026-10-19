"""Verify PingFang, account button contrast, device login and circuit release."""

import asyncio
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from browser_config import taobao_browser_options

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8765"
LOGIN_URL = "https://login.taobao.com/havanaone/login/login.htm"


async def main():
    with tempfile.TemporaryDirectory(prefix="account-controls-qa-") as temporary:
        os.environ["MONITOR_DATA_DIR"] = temporary
        os.environ["MONITOR_DISABLE_WORKER"] = "1"
        from backend import app as server, access, collector, domain as d, store as s

        s.init()
        access.rotate_read_only_token()
        errors, login_calls = [], []
        collector.LOGIN.update(
            status="waiting",
            message="请在服务器 Mac 登录后保存",
            issue=None,
            navigating=False,
        )

        async def fake_login():
            collector.LOGIN.update(
                status="waiting",
                message="测试服务器窗口已打开",
                issue=None,
                navigating=False,
            )

        collector.login = fake_login
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app, client=("127.0.0.1", 12345))
        ) as local:
            await local.get(URL + "/api/bootstrap")
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, **taobao_browser_options("chrome")
                )
                try:
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 832}
                    )
                    page = await context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    async def intercept(route):
                        request = route.request
                        if request.url.startswith(LOGIN_URL):
                            await route.fulfill(
                                status=200,
                                content_type="text/html",
                                body="<!doctype html><title>隔离登录页测试</title>",
                            )
                            return
                        if not request.url.startswith(
                            URL + "/"
                        ) or request.url.endswith("/api/stream"):
                            await route.abort()
                            return
                        if request.url.endswith("/api/session/login"):
                            login_calls.append(request.url)
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

                    await context.route("**/*", intercept)
                    output = ROOT / "output/account-controls-qa"
                    output.mkdir(parents=True, exist_ok=True)
                    contrast = {}
                    for theme in ("light", "dark"):
                        s.set_settings({"theme": theme})
                        await page.goto(
                            URL + "/settings?tab=account", wait_until="domcontentloaded"
                        )
                        complete = page.get_by_role(
                            "button", name="完成登录并保存", exact=True
                        )
                        await complete.wait_for()
                        await page.wait_for_timeout(500)
                        assert await page.locator(".app-shell").evaluate(
                            "el=>getComputedStyle(el).fontFamily.startsWith('\"PingFang SC\"')"
                        )
                        ratio = await complete.evaluate("""el => {
                          const parse = value => (value.match(/[\\d.]+/g)||[]).map(Number);
                          const rgb = value => {const a=parse(value); return [a[0],a[1],a[2],a.length>3?a[3]:1];};
                          let background=[0,0,0], opacity=0;
                          for(let node=el;node && opacity<.999;node=node.parentElement){
                            const layer=rgb(getComputedStyle(node).backgroundColor);
                            const weight=(1-opacity)*layer[3];
                            background=background.map((v,i)=>v+layer[i]*weight); opacity+=weight;
                          }
                          if(opacity<1)background=background.map(v=>v+255*(1-opacity));
                          const luminance = a => a.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
                          const foreground=luminance(rgb(getComputedStyle(el).color)), bg=luminance(background);
                          return (Math.max(foreground,bg)+.05)/(Math.min(foreground,bg)+.05);
                        }""")
                        assert ratio >= 4.5, (theme, ratio)
                        assert (
                            await complete.evaluate(
                                "el=>getComputedStyle(el).backgroundColor"
                            )
                            == "rgba(0, 0, 0, 0)"
                        )
                        contrast[theme] = round(ratio, 2)
                        strong_color = await page.locator(
                            ".account-details strong"
                        ).first.evaluate("el=>getComputedStyle(el).color")
                        assert strong_color == await page.locator(
                            ".page-heading h1"
                        ).evaluate("el=>getComputedStyle(el).color")
                        for width in (1280, 390):
                            await page.set_viewport_size(
                                {"width": width, "height": 832}
                            )
                            await page.wait_for_timeout(200)
                            assert await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth+2"
                            ), (theme, width)
                            await page.screenshot(
                                path=str(output / f"account-{theme}-{width}.png"),
                                full_page=True,
                            )
                        await page.set_viewport_size({"width": 1280, "height": 832})

                    cdp = await context.new_cdp_session(page)
                    await cdp.send("DOM.enable")
                    await cdp.send("CSS.enable")
                    doc = await cdp.send("DOM.getDocument")
                    node = await cdp.send(
                        "DOM.querySelector",
                        {
                            "nodeId": doc["root"]["nodeId"],
                            "selector": ".page-heading h1",
                        },
                    )
                    fonts = (
                        await cdp.send(
                            "CSS.getPlatformFontsForNode", {"nodeId": node["nodeId"]}
                        )
                    )["fonts"]
                    assert any(
                        "PingFang" in font["familyName"] for font in fonts
                    ), fonts

                    async with page.expect_popup() as opened:
                        await page.get_by_role(
                            "link", name="在本设备打开淘宝登录页", exact=True
                        ).click()
                    popup = await opened.value
                    await popup.wait_for_load_state("domcontentloaded")
                    assert popup.url == LOGIN_URL
                    assert not login_calls
                    await popup.close()
                    await page.get_by_text(
                        "用于采集的登录窗口在服务器 Mac 上打开。", exact=False
                    ).wait_for()

                    collector.LOGIN.update(status="idle", message="尚未登录")
                    await page.reload(wait_until="domcontentloaded")
                    await page.get_by_role(
                        "button", name="在服务器 Mac 登录", exact=True
                    ).click()
                    await page.get_by_role(
                        "button", name="完成登录并保存", exact=True
                    ).wait_for()
                    assert len(login_calls) == 1

                    collector.LOGIN.update(status="idle", message="尚未登录")
                    s.set_settings(
                        {
                            "circuit_until": (
                                d.utcnow() + timedelta(hours=1)
                            ).isoformat(),
                            "failures": 5,
                        }
                    )
                    await page.goto(URL + "/collection", wait_until="domcontentloaded")
                    release = page.get_by_role("button", name="解除熔断", exact=True)
                    await release.wait_for()
                    assert await release.is_enabled()
                    await release.click()
                    await page.get_by_text(
                        "熔断已解除，连续失败计数已清零", exact=True
                    ).wait_for()
                    assert (
                        s.settings()["circuit_until"] is None
                        and s.settings()["failures"] == 0
                    )
                    assert await release.is_disabled()
                    await page.get_by_role(
                        "button", name="手动熔断", exact=True
                    ).click()
                    await page.get_by_text("已手动熔断", exact=True).wait_for()
                    assert await release.is_enabled()
                    await release.click()
                    assert (
                        await page.get_by_label(
                            "最大并发采集数", exact=True
                        ).input_value()
                        == "5"
                    )
                    assert not errors, errors
                    report = {
                        "font": "PingFang SC",
                        "actual_fonts": fonts,
                        "ghost_button_contrast": contrast,
                        "device_login_opens_current_browser": True,
                        "server_login_separate": True,
                        "manual_uncircuit": True,
                        "widths": [1280, 390],
                        "page_errors": errors,
                    }
                    (output / "report.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2)
                    )
                    print(json.dumps(report, ensure_ascii=False))
                finally:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
