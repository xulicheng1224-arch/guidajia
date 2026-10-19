"""Verify the real Edge extension using a fresh profile and intercepted pages.

No user browser profile, real credentials, production DB, or live site requests.
Run explicitly: .venv/bin/python tests/cookie_extension_fixture.py
"""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def exercise():
    extension = Path(__file__).resolve().parent.parent / "tools/cookie-extension"
    # Chromium's unpacked-extension ID is derived from the absolute path.
    extension_id = "".join(chr(ord("a") + int(c, 16)) for c in
                           hashlib.sha256(str(extension).encode()).hexdigest()[:32])
    with tempfile.TemporaryDirectory(prefix="cookie-extension-fixture-") as folder:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                folder, channel="msedge", headless=True,
                ignore_default_args=["--disable-extensions"],
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            )
            try:
                async def intercept(route):
                    url = urlsplit(route.request.url)
                    body = "<html><body>fixture"
                    if url.hostname == "www.taobao.com":
                        login = "login" if url.path == "/first" else "passport"
                        body += f'<iframe src="https://{login}.taobao.com/fixture"></iframe>'
                        body += '<iframe src="https://unrelated.example/fixture"></iframe>'
                    body += "<script>localStorage.setItem('fixture-storage', 'fixture-value')</script></body></html>"
                    await route.fulfill(status=200, content_type="text/html", body=body)

                await context.route("https://**/*", intercept)
                cookie = {"name": "unb", "value": "fixture-ordinary", "domain": ".taobao.com",
                          "path": "/", "secure": True, "httpOnly": True, "sameSite": "None"}
                await context.add_cookies([
                    cookie,
                    {**cookie, "value": "fixture-partitioned-true", "partitionKey": "https://tmall.com", "_crHasCrossSiteAncestor": True},
                    {**cookie, "value": "fixture-partitioned-false", "partitionKey": "https://tmall.com", "_crHasCrossSiteAncestor": False},
                ])
                for path in ("first", "second"):
                    page = await context.new_page()
                    await page.goto(f"https://www.taobao.com/{path}")
                popup = await context.new_page()
                await popup.goto(f"chrome-extension://{extension_id}/popup.html")
                result = await popup.evaluate("""async () => {
                    const { exportCookies } = await import('./cookies.mjs');
                    return await exportCookies(chrome);
                }""")
                state = result["state"]
                assert len(state["cookies"]) == 3, "Cookie count changed"
                assert {c["value"] for c in state["cookies"]} == {
                    "fixture-ordinary", "fixture-partitioned-true", "fixture-partitioned-false"}
                assert all(c["httpOnly"] for c in state["cookies"])
                assert {c.get("_crHasCrossSiteAncestor") for c in state["cookies"] if c.get("partitionKey")} == {True, False}
                assert {o["origin"] for o in state["origins"]} == {
                    "https://www.taobao.com", "https://login.taobao.com", "https://passport.taobao.com"}, "Iframe storage missing"
                assert all(o["localStorage"] == [{"name": "fixture-storage", "value": "fixture-value"}] for o in state["origins"])
                info = state["export_info"]
                assert info["storage_read_failures"] == 0
                assert info["storage_frames_read"] == 4
                assert info["missing_auth_storage_origins"] == []
                print(json.dumps({"extension_version": info["version"], "cookie_count": len(state["cookies"]),
                                  "storage_origin_count": len(state["origins"]), "storage_frames_read": info["storage_frames_read"],
                                  "passed": True}))
            finally:
                await context.close()


if __name__ == "__main__":
    asyncio.run(exercise())
