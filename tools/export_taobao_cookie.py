#!/usr/bin/env python3
"""Open a local Taobao login window and export an upload-ready Cookie JSON."""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from browser_config import taobao_browser_options

LOGIN_URL = "https://login.taobao.com/havanaone/login/login.htm"
ALLOWED_ROOTS = ("taobao.com", "tmall.com")


def allowed_host(value):
    host = str(value or "").lower().lstrip(".")
    return any(host == root or host.endswith("." + root) for root in ALLOWED_ROOTS)


def upload_state(state):
    """Keep only Taobao/Tmall cookies and local storage from those origins."""
    cookies = [
        cookie
        for cookie in state.get("cookies", [])
        if isinstance(cookie, dict) and allowed_host(cookie.get("domain"))
    ]
    origins = []
    for origin in state.get("origins", []):
        if not isinstance(origin, dict):
            continue
        if not allowed_host(urlsplit(str(origin.get("origin", ""))).hostname):
            continue
        origins.append(origin)
    return {"cookies": cookies, "origins": origins}


def output_path(value):
    if value:
        return Path(value).expanduser().resolve()
    desktop = Path.home() / "Desktop"
    folder = desktop if desktop.is_dir() else Path.home() / "Downloads"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return folder / f"taobao-cookie-{stamp}.json"


async def export(browser_name, destination):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("找不到 Playwright，请先安装项目依赖：python -m pip install -r requirements.txt")
        return 2

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                headless=False, **taobao_browser_options(browser_name)
            )
        except Exception as exc:
            print(
                f"无法启动 {browser_name}：{type(exc).__name__}。"
                "请确认浏览器已安装，或改用 --browser chrome。"
            )
            return 2
        context = await browser.new_context(locale="zh-CN")
        page = await context.new_page()
        try:
            print("正在打开淘宝登录页……")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"登录页打开失败：{type(exc).__name__}。浏览器窗口仍会保留，请检查网络。")
        print("请在打开的浏览器窗口中完成淘宝登录。")
        print("登录完成后回到此终端按回车，脚本会生成可上传的 JSON 文件。")
        await asyncio.to_thread(input)
        state = upload_state(await context.storage_state(indexed_db=True))
        if not state["cookies"]:
            print("没有检测到淘宝 / 天猫 Cookie，未生成文件。请确认已登录后重试。")
            await browser.close()
            return 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
        destination.chmod(0o600)
        print(f"已导出 {len(state['cookies'])} 条淘宝 / 天猫 Cookie。")
        print(f"文件：{destination}")
        print("下一步：在鬼打架「设置 → 淘宝账号 → 上传 Cookie 文件」选择这个文件。")
        await browser.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser",
        choices=("msedge", "chrome", "chromium"),
        default="msedge",
        help="用于登录的本机浏览器，默认 Microsoft Edge",
    )
    parser.add_argument("--output", type=Path, help="输出 JSON 路径，默认桌面或 Downloads")
    args = parser.parse_args()
    return asyncio.run(export(args.browser, output_path(args.output)))


if __name__ == "__main__":
    raise SystemExit(main())
