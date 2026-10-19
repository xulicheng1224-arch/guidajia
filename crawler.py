"""淘宝 / 天猫 DOM 价格采集。失败 price=None；不使用商品价格 API。"""

import argparse
import asyncio
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from crawlee.configuration import Configuration
from crawlee.crawlers import PlaywrightCrawler
from lxml import html
from browser_config import taobao_browser_options, taobao_context_options
from diagnostics import describe, exception_detail, product_url
from collection_limits import COLLECT_TIMEOUT_SECONDS
from crawler_diagnostics import PageDiagnostics

BASE = Path(__file__).resolve().parent
PRICE_SELECTOR = '[class*="normalPrice--"], [class*="Price--price"], [class*="priceWrap--"], [class*="priceWrapper--"], #tbpcDetail_SkuPanelBody [class*="block2--"] > div:has(> [class*="unit--"])'
ITEM_HOSTS = {"detail.tmall.com", "item.taobao.com"}
LOGIN_HOSTS = {"login.taobao.com", "login.tmall.com", "passport.taobao.com"}
SELECTED_SELECTOR = ",".join(
    f'[class*="{item}"][class*="{state}"]'
    for item in ("skuItem", "valueItem")
    for state in ("selected", "Selected")
)


def parse_price(text):
    masked = bool(re.search(r"\d\s*[?？]", text))
    normalized = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text)
    normalized = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", normalized)
    original = re.search(
        r"(?:优惠前|原价|划线价)\s*[￥¥]\s*(\d+(?:\.\d{1,2})?)(?![\d.,?？])", normalized
    )
    sale = re.search(
        r"(?:平台加补后|到手价|券后价|活动价|折后价|淘宝价|促销价)\s*[￥¥]?\s*(\d+(?:\.\d{1,2})?)(?![\d.,?？])",
        normalized,
    )
    # Only a single currency amount in the selected price region is unambiguous.
    amounts = re.findall(r"[￥¥]\s*(\d+(?:\.\d{1,2})?)(?![\d.,?？])", normalized)
    plain = amounts[0] if not sale and not original and len(amounts) == 1 else None
    return {
        "display_text": text,
        "masked": masked,
        "sale_price": None if masked else (sale.group(1) if sale else plain),
        "original_price": original.group(1) if original else None,
        "price_type": (
            "coupon"
            if "券后" in text or "领券" in text
            else ("member" if "会员" in text else "regular")
        ),
    }


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checkpoint(args, result, stage=None):
    """Write a small, secret-free progress snapshot for the parent process."""
    path = getattr(args, "progress", None)
    if not path:
        return
    if stage:
        result["diagnostics"]["stage"] = stage
    snapshot = {
        "status": result.get("status"),
        "price": None,
        "final_host": result.get("final_host"),
        "diagnostics": dict(result.get("diagnostics") or {}),
    }
    save_json(Path(path), snapshot)


def is_login_url(value):
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return host in LOGIN_HOSTS and (
        host != "login.taobao.com" or "login" in path or "havana" in path
    )


class LoginRedirectMonitor:
    """Allow a bounded SSO handoff before calling a login-domain visit a failure."""

    def __init__(self, args, result, grace_seconds=5):
        self.args, self.result = args, result
        self.grace_seconds = grace_seconds
        self.candidate_since = None
        self.previous_url = None
        self.interrupted = asyncio.Event()

    def observe(self, url):
        if self.args.login:
            return
        now = time.monotonic()
        info = self.result["diagnostics"]
        if info.get("verification_required"):
            return
        if is_login_url(url):
            if self.candidate_since is None:
                self.candidate_since = now
                info.update(stage="login_handoff", login_detected=False,
                            login_redirect_seen=True, login_grace_seconds=self.grace_seconds)
            if now - self.candidate_since >= self.grace_seconds:
                self.result.update(status="login_or_redirect", price=None)
                info.update(stage="login_redirect", login_detected=True,
                            login_confirmation="login_host_persisted",
                            login_wait_ms=int((now - self.candidate_since) * 1000))
                self.interrupted.set()
        else:
            if self.candidate_since is not None:
                info.update(login_detected=False, login_handoff_completed=True, stage="navigation")
                if self.result.get("status") == "login_or_redirect":
                    self.result["status"] = "crawler_error"
            self.candidate_since = None
        if url != self.previous_url or info.get("login_detected"):
            self.previous_url = url
            self.result["final_host"] = urlparse(url).hostname
            info["final_url"] = product_url(url)
            checkpoint(self.args, self.result)

    async def settle(self, page):
        await self.read_verification(page)
        if self.result["diagnostics"].get("verification_required"):
            return True
        while not self.args.login and is_login_url(page.url):
            await self.read_verification(page)
            self.observe(page.url)
            if (self.result["diagnostics"].get("login_detected")
                    or self.result["diagnostics"].get("verification_required")):
                return True
            await asyncio.sleep(0.1)
        self.observe(page.url)
        return False


    async def wait_render(self, seconds):
        """Stop waiting immediately when navigation or a challenge is confirmed."""
        if self.interrupted.is_set():
            return True
        try:
            await asyncio.wait_for(self.interrupted.wait(), timeout=max(0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def watch(self, page):
        while not page.is_closed():
            await self.read_verification(page)
            self.observe(page.url)
            await asyncio.sleep(0.5)

    async def read_verification(self, page):
        if self.args.login or urlparse(page.url).hostname not in ITEM_HOSTS | LOGIN_HOSTS:
            return
        if self.result["diagnostics"].get("verification_required"):
            return
        url = page.url
        try:
            # Return fixed signals only, never raw body/phone/account/URL tokens.
            evidence = await asyncio.wait_for(page.evaluate("""() => {
                const text = document.body?.innerText || '';
                const title = document.title || '';
                const challengeTitle = ['验证码拦截', '安全验证', '身份验证', '访问验证', '访问受限']
                    .find(s => title.includes(s));
                const sms = text.includes('短信') && text.includes('验证码') &&
                    (location.hostname.includes('passport.') || location.hostname.includes('login.') || !!challengeTitle);
                const slider = ['拖动滑块', '请按住滑块'].some(s => text.includes(s));
                const prompt = ['请完成验证', '访问被限制', '请通过验证后继续'].some(s => text.includes(s));
                return {kind: sms ? 'sms' : slider ? 'slider' : challengeTitle || prompt ? 'captcha' : null,
                    signals: ['短信', '验证码', '异常', '拖动滑块', '请按住滑块', '请完成验证', '访问被限制']
                        .filter(s => text.includes(s)),
                    challenge_title: challengeTitle || null};
            }"""), timeout=0.5)
        except Exception:
            return
        if page.url != url or not evidence.get("kind"):
            return
        info = self.result["diagnostics"]
        self.result.update(status="blocked", price=None, final_host=urlparse(url).hostname)
        info.update(stage="security_verification", verification_required=True,
                    verification_kind=evidence["kind"],
                    verification_signals=evidence.get("signals", []),
                    login_detected=False, final_url=product_url(url))
        if evidence.get("challenge_title"):
            info["title"] = evidence["challenge_title"]
        self.interrupted.set()
        checkpoint(self.args, self.result)


def visible_prices(page):
    return page.locator(", ".join(part.strip() + ":visible" for part in PRICE_SELECTOR.split(",")))


async def wait_price_region(page, monitor, seconds):
    """Race price readiness against login/CAPTCHA detection, preserving its code."""
    if monitor.interrupted.is_set():
        return False
    visible = asyncio.create_task(visible_prices(page).first.wait_for(state="visible", timeout=max(1,int(seconds*1000))))
    interrupted = asyncio.create_task(monitor.interrupted.wait())
    try:
        await asyncio.wait((visible, interrupted), return_when=asyncio.FIRST_COMPLETED)
        if monitor.interrupted.is_set():
            return False
        try:
            visible.result()
        except Exception as exc:
            if type(exc).__name__ == "TimeoutError":
                raise TimeoutError("等待价格区域出现超时") from None
            raise
        return not await monitor.settle(page)
    finally:
        for task in (visible, interrupted):
            if not task.done():
                task.cancel()
        await asyncio.gather(visible, interrupted, return_exceptions=True)


async def sku_evidence(page, requested):
    # Only inspect the page's rendered selection and its embedded SKU metadata.
    # Prices are still read exclusively from visible DOM, never from an API.
    evidence = await page.evaluate(
        """selector => {
      const input = document.querySelector('input[name="skuId"]');
      if (input?.value && /^\\d+$/.test(input.value))
        return {selected_sku_id: input.value, sku_verification: 'dom_input'};
      const nodes = [...document.querySelectorAll(selector)].filter(n => n.getClientRects().length && n.dataset.vid);
      const vids = [...new Set(nodes.map(n => n.dataset.vid))];
      const base = window.__ICE_APP_CONTEXT__?.loaderData?.home?.data?.res?.skuBase;
      if (!base || !Array.isArray(base.skus) || !Array.isArray(base.props) || vids.length !== base.props.length)
        return {selected_option_ids: vids};
      if (!base.props.every(prop => Array.isArray(prop.values) && prop.values.some(value => vids.includes(String(value.vid)))))
        return {selected_option_ids: vids};
      const matches = base.skus.filter(sku => {
        const pairs = String(sku.propPath || '').split(';').filter(Boolean);
        return pairs.length === vids.length && pairs.every(pair => vids.includes(pair.split(':')[1]));
      });
      if (matches.length !== 1 || !/^\\d+$/.test(String(matches[0].skuId)))
        return {selected_option_ids: vids};
      return {selected_sku_id: String(matches[0].skuId), sku_verification: 'page_sku_mapping', selected_option_ids: vids, selected_prop_path: matches[0].propPath};
    }""",
        SELECTED_SELECTOR,
    )
    return {
        **evidence,
        "sku_verified": not requested or evidence.get("selected_sku_id") == requested,
    }


async def crawl(args):
    started = time.monotonic()
    timeout = getattr(args, "timeout", COLLECT_TIMEOUT_SECONDS)
    args._deadline = started + (1200 if args.login else timeout)
    query = parse_qs(urlparse(args.url).query)
    result = {
        "platform": (
            "tmall" if urlparse(args.url).hostname == "detail.tmall.com" else "taobao"
        ),
        "framework": "Crawlee",
        "item_id": query["id"][0],
        "requested_sku_id": query.get("skuId", [None])[0],
        "headless": not (args.login or args.headed),
        "price": None,
        "sku_verified": False,
        "status": "crawler_error",
        "diagnostics": {
            "stage": "browser_launch",
            "browser": args.browser,
            "wait_seconds": args.wait,
            "timeout_seconds": timeout,
        },
    }
    checkpoint(args, result, "browser_launch")
    try:
        if args.login:
            await crawl_browser(args, result)
        else:
            async with asyncio.timeout(timeout):
                await crawl_browser(args, result)
    except TimeoutError:
        if not result["diagnostics"].get("verification_required") and result.get("status") != "login_or_redirect":
            result.update(price=None, status="timeout")
            result["diagnostics"]["exception_message"] = f"采集达到 {timeout} 秒总时限"
    except Exception as error:
        if result.get("status") != "login_or_redirect" and not result.get("diagnostics", {}).get("verification_required"):
            result.update(
                price=None,
                status=(
                    "setup_error"
                    if result["diagnostics"].get("stage") == "browser_launch"
                    else "crawler_error"
                ),
            )
        result["diagnostics"].update(
            exception_detail(error, result["diagnostics"].get("stage"))
        )
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    result["diagnostics"] = describe(result)
    if result["status"] != "success":
        result["price"] = None
    result["diagnostics"]["duration_ms"] = int((time.monotonic() - started) * 1000)
    checkpoint(args, result, result["diagnostics"].get("stage"))
    save_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 1


async def crawl_browser(args, result):
    query = parse_qs(urlparse(args.url).query)
    options = taobao_context_options()
    # Crawlee otherwise installs generated global Accept / User-Agent / client
    # hints even with fingerprint_generator=None. Keep each resource request's
    # native browser headers, consistent with the saved-login browser.
    options["extra_http_headers"] = {}
    if args.state.is_file():
        options["storage_state"] = str(args.state)
    launch = taobao_browser_options(args.browser)
    monitor = LoginRedirectMonitor(args, result)
    login_watch = None
    page_diagnostics = None
    logging.disable(logging.CRITICAL)  # 不在日志中输出完整请求 URL / 会话数据。
    with tempfile.TemporaryDirectory(prefix="tmall-crawlee-") as storage:
        crawler = PlaywrightCrawler(
            headless=result["headless"],
            fingerprint_generator=None,
            use_incognito_pages=True,
            browser_launch_options=launch,
            browser_new_context_options=options,
            max_requests_per_crawl=1,
            max_request_retries=0,
            max_session_rotations=0,
            use_session_pool=False,
            retry_on_blocked=False,
            configure_logging=False,
            navigation_timeout=timedelta(seconds=45),
            request_handler_timeout=timedelta(seconds=1200 if args.login else getattr(args,"timeout",COLLECT_TIMEOUT_SECONDS)),
            goto_options={"wait_until": "domcontentloaded"},
            configuration=Configuration(storage_dir=storage, purge_on_start=False),
        )

        @crawler.pre_navigation_hook
        async def navigation_start(ctx):
            nonlocal login_watch, page_diagnostics
            page_diagnostics = PageDiagnostics(ctx.page)
            result["diagnostics"]["stage"] = "navigation"
            # Confirm Playwright received the imported state, without logging
            # values or account IDs. Only cookie counts are retained.
            loaded = await ctx.page.context.cookies()
            applicable = await ctx.page.context.cookies(args.url)
            result["diagnostics"].update(
                session_state_loaded=args.state.is_file(),
                loaded_cookie_count=len(loaded),
                product_cookie_count=len(applicable),
            )
            checkpoint(args, result)

            def on_frame(frame):
                if frame == ctx.page.main_frame:
                    monitor.observe(frame.url)

            ctx.page.on("framenavigated", on_frame)
            if login_watch is not None:
                login_watch.cancel()
                await asyncio.gather(login_watch, return_exceptions=True)
            login_watch = asyncio.create_task(monitor.watch(ctx.page))

        @crawler.post_navigation_hook
        async def navigation_observed(ctx):
            await monitor.read_verification(ctx.page)
            if await monitor.settle(ctx.page):
                return
            host = urlparse(ctx.page.url).hostname
            result["final_host"] = host
            result["diagnostics"].update(
                http_status=ctx.response.status if ctx.response else None,
                final_url=product_url(ctx.page.url),
            )
            if host not in ITEM_HOSTS and not is_login_url(ctx.page.url):
                result["status"] = "login_or_redirect"
                result["diagnostics"].update(
                    stage="login_redirect" if is_login_url(ctx.page.url) else "navigation",
                    login_detected=is_login_url(ctx.page.url),
                )
            checkpoint(args, result)

        @crawler.router.default_handler
        async def handle(ctx):
            page = ctx.page
            if args.login:
                print(
                    "请在打开的浏览器中完成登录，返回本终端按 Enter 继续。", flush=True
                )
                await asyncio.to_thread(input)
                targets = [
                    p
                    for p in page.context.pages
                    if urlparse(p.url).hostname in ITEM_HOSTS
                ]
                if targets:
                    page = targets[-1]
                await page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
                result["status"] = "crawler_error"
                result["diagnostics"]["login_detected"] = False
            if result["diagnostics"].get("verification_required"):
                return
            # 等待异步渲染；不是仅等待 HTML 下载。
            result["diagnostics"]["stage"] = "render"
            checkpoint(args, result)
            if await monitor.settle(page):
                return
            # The configured render wait is an upper bound. A visible price can
            # proceed early; always reserve time for parsing and cleanup.
            render_wait = min(args.wait, max(0, getattr(args, "_deadline", time.monotonic()+COLLECT_TIMEOUT_SECONDS)-time.monotonic()-6))
            if render_wait:
                try:
                    if not await wait_price_region(page, monitor, render_wait):
                        if page_diagnostics:
                            await page_diagnostics.capture(result)
                        return
                except TimeoutError:
                    pass
            if await monitor.settle(page):
                return
            parsed = urlparse(page.url)
            current = parse_qs(parsed.query)
            result.update(
                final_host=parsed.hostname,
                title=await page.title(),
                url_item_matches=current.get("id") == query.get("id"),
                url_sku_matches=bool(query.get("skuId"))
                and current.get("skuId") == query.get("skuId"),
            )
            result["diagnostics"]["final_url"] = product_url(page.url)
            if parsed.hostname not in ITEM_HOSTS or is_login_url(page.url):
                result["status"] = "login_or_redirect"
                result["diagnostics"].update(
                    stage="login_redirect" if is_login_url(page.url) else "navigation",
                    login_detected=is_login_url(page.url),
                )
                checkpoint(args, result)
                return
            if not result["url_item_matches"]:
                result["status"] = "item_mismatch"
                return
            body = await page.locator("body").inner_text()
            if page_diagnostics:
                await page_diagnostics.capture(result)
            await monitor.read_verification(page)
            if result["diagnostics"].get("verification_required"):
                return
            if any(
                word in body for word in ("宝贝不存在", "商品不存在", "宝贝已被删除")
            ):
                result["status"] = "page_missing"
                result["stock"] = "unknown"
                return
            if any(
                word in body for word in ("该宝贝已下架", "商品已下架", "宝贝已下架")
            ):
                result["stock"] = "out_of_stock"
            result["selected_options"] = [
                re.sub(r"\s+", " ", text).strip()[:300]
                for text in await page.locator(SELECTED_SELECTOR).all_text_contents()
            ]
            result.update(await sku_evidence(page, result["requested_sku_id"]))
            result["diagnostics"]["stage"] = "price_region"
            checkpoint(args, result)
            nodes = visible_prices(page)
            try:
                # Previously this was a separate hard 30-second limit, so a
                # larger process timeout could never help a slowly rendered page.
                remaining = max(0.1, getattr(args,"_deadline",time.monotonic()+COLLECT_TIMEOUT_SECONDS)-time.monotonic()-6)
                if not await wait_price_region(page, monitor, remaining):
                    if page_diagnostics:
                        await page_diagnostics.capture(result)
                    return
            except Exception as error:
                if page_diagnostics:
                    await page_diagnostics.capture(result)
                # The title/page may have changed while waiting. Do not replace
                # a late CAPTCHA or login redirect with "price region missing".
                if await monitor.settle(page):
                    return
                result["status"] = "price_region_missing"
                result["diagnostics"].update(exception_detail(error, "price_region"))
                result["diagnostics"]["price_selector"] = PRICE_SELECTOR
                result["diagnostics"]["price_nodes"] = await nodes.count()
                return
            parsed = urlparse(page.url)
            result["diagnostics"]["final_url"] = product_url(page.url)
            result["final_host"] = parsed.hostname
            if parsed.hostname not in ITEM_HOSTS:
                result["status"] = "login_or_redirect"
                return
            if parse_qs(parsed.query).get("id") != query.get("id"):
                result["status"] = "item_mismatch"
                return
            body = await page.locator("body").inner_text()
            result["selected_options"] = [re.sub(r"\s+", " ", text).strip()[:300]
                for text in await page.locator(SELECTED_SELECTOR).all_text_contents()]
            # 同一页面可能存在用于不同布局的重复节点，优先可见区域。
            node = nodes.first
            for index in range(await nodes.count()):
                candidate = nodes.nth(index)
                if await candidate.is_visible():
                    node = candidate
                    break
            markup = await node.evaluate("(n) => n.outerHTML")
            result["diagnostics"].update(
                stage="price_parse",
                price_nodes=await nodes.count(),
                price_selector=PRICE_SELECTOR,
            )
            text = await node.inner_text()
            if page_diagnostics:
                await page_diagnostics.capture(result)
            result["diagnostics"]["price_text"] = text[:500]
            soup = BeautifulSoup(markup, "html.parser")
            tree = html.fromstring(markup)
            texts = {
                "bs4": soup.get_text(" ", strip=True),
                "xpath": " ".join(
                    t.strip() for t in tree.xpath("//text()") if t.strip()
                ),
                "re": text,
            }
            result["methods"] = {
                key: parse_price(value) for key, value in texts.items()
            }
            values = list(result["methods"].values())
            amounts = {v["sale_price"] for v in values if v["sale_price"] is not None}
            result["price"] = (
                next(iter(amounts))
                if len(amounts) == 1 and not any(v["masked"] for v in values)
                else None
            )
            result["original_price"] = next(
                (v["original_price"] for v in values if v["original_price"]), None
            )
            result["price_type"] = next((v["price_type"] for v in values), "regular")
            result["stock"] = result.get("stock") or (
                "out_of_stock"
                if any(
                    word in body
                    for word in ("暂时无货", "该规格已售罄", "此规格已售罄")
                )
                else "in_stock" if result["price"] else "unknown"
            )
            # Never infer SKU identity from the URL alone; the monitor can verify
            # the observed selected options against a user's expected specification.
            result.update(await sku_evidence(page, result["requested_sku_id"]))
            result["status"] = (
                "price_masked"
                if any(v["masked"] for v in values)
                else ("success" if result["price"] else "price_missing")
            )
            # URL 保留 skuId 不能证明页面选项和该 SKU 的映射，故不自动确认成交价。
            result["diagnostics"]["stage"] = "session_save"
            save_json(args.state, await page.context.storage_state(indexed_db=True))
            os.chmod(args.state, 0o600)
            result["session_state_saved"] = True
            result["diagnostics"]["stage"] = (
                "complete" if result["status"] == "success" else "price_parse"
            )

        @crawler.failed_request_handler
        async def failed(ctx, error):
            if page_diagnostics:
                await page_diagnostics.capture(result)
            if await monitor.settle(ctx.page):
                checkpoint(args, result)
                return
            if result["status"] != "login_or_redirect" and not result["diagnostics"].get("verification_required"):
                result.update(status="crawler_error", error_type=type(error).__name__)
            result["diagnostics"].update(
                exception_detail(
                    error, result["diagnostics"].get("stage", "navigation")
                )
            )
            checkpoint(args, result)

        try:
            await crawler.run([args.url])
        except Exception as error:
            if result.get("status") != "login_or_redirect" and not result["diagnostics"].get("verification_required"):
                result.update(status="setup_error", error_type=type(error).__name__)
            result["diagnostics"].update(
                exception_detail(
                    error, result["diagnostics"].get("stage", "browser_launch")
                )
            )
            checkpoint(args, result)
        finally:
            if login_watch is not None:
                login_watch.cancel()
                await asyncio.gather(login_watch, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="天猫商品完整 URL（用引号括起）")
    parser.add_argument(
        "--login", action="store_true", help="打开浏览器手动登录，终端回车后采集"
    )
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument(
        "--browser", choices=("msedge", "chrome", "chromium"), default="msedge"
    )
    parser.add_argument(
        "--wait", type=float, default=15, help="页面渲染等待秒数，默认 15"
    )
    parser.add_argument("--timeout", type=float, default=COLLECT_TIMEOUT_SECONDS,
                        help="单次采集总时限，默认 120 秒")
    parser.add_argument(
        "--state", type=Path, default=BASE / "session.json", help="本机登录会话路径"
    )
    parser.add_argument(
        "--output", type=Path, default=BASE / "result.json", help="JSON 结果路径"
    )
    parser.add_argument(
        "--progress", type=Path, default=None, help="父进程读取的无敏感进度快照"
    )
    args = parser.parse_args()
    url = urlparse(args.url)
    if (
        url.scheme != "https"
        or url.hostname not in ITEM_HOSTS
        or not parse_qs(url.query).get("id")
    ):
        parser.error("请提供带 id 参数的淘宝 / 天猫 HTTPS 商品链接")
    if not 0 <= args.wait <= COLLECT_TIMEOUT_SECONDS:
        parser.error("--wait 必须在 0 到 120 秒之间")
    if not 0 < args.timeout <= COLLECT_TIMEOUT_SECONDS:
        parser.error("--timeout 必须大于 0 且不超过 120 秒")
    return asyncio.run(crawl(args))


if __name__ == "__main__":
    raise SystemExit(main())
