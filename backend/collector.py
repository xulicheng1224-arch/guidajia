"""Browser-only price branch, running Crawlee in an isolated process."""

import asyncio
import json
import os
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit

from browser_config import taobao_browser_options, taobao_context_options
from collection_limits import COLLECT_TIMEOUT_SECONDS
from diagnostics import describe, exception_detail

from . import store as s
from . import session_state
from . import session_verification, public_view
from .product_urls import normalize_url, TAOBAO_HOSTS
from .process_tree import CrawlerProcessTree

SESSION = s.data_dir() / "auth" / "taobao.json"
SESSION.parent.mkdir(parents=True, exist_ok=True)
os.chmod(SESSION.parent, 0o700)
def initial_login(path):
    state = {"status": "idle", "message": "尚未登录", "started_at": None,
             "issue": None, "navigating": False}
    if path.is_file():
        state.update(status="saved", message="已保存登录状态，采集时自动复用")
        try:
            verification = json.loads(path.with_name("verification.json").read_text())
        except (OSError, ValueError):
            verification = {}
        if verification.get("state") in ("login_required", "verification_required"):
            state.update(status="expired", issue=verification["state"],
                         message=verification.get("message") or "当前会话需要登录或人工验证")
    return state


LOGIN = initial_login(SESSION)
login_task = None
login_done = asyncio.Event()
login_retry = asyncio.Event()
SESSION_LOCK = asyncio.Lock()
LOGIN_URL = "https://login.taobao.com/havanaone/login/login.htm"
LOGIN_TIMEOUT_SECONDS = 900


_TENANTS = {}

def runtime():
    if s.TENANT.get() == "admin":
        return sys.modules[__name__]
    key = str(s.data_dir())
    if key not in _TENANTS:
        from types import SimpleNamespace
        path = s.data_dir() / "auth" / "taobao.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _TENANTS[key] = SimpleNamespace(SESSION=path, LOGIN=initial_login(path),
            login_task=None, login_done=asyncio.Event(), login_retry=asyncio.Event(), SESSION_LOCK=asyncio.Lock())
    return _TENANTS[key]


def cents(value):
    if value is None or value == "":
        return None
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0 or n > 100000000:
            return None
        amount = int((n * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return amount if amount > 0 else None
    except (InvalidOperation, ValueError):
        return None


def session_info():
    result = {**runtime().LOGIN, "exists": runtime().SESSION.is_file(), "saved_at": None}
    result["verification"] = session_verification.read(runtime().SESSION)
    if runtime().SESSION.is_file():
        from datetime import datetime, timezone

        result["saved_at"] = datetime.fromtimestamp(
            runtime().SESSION.stat().st_mtime, timezone.utc
        ).isoformat()
        if runtime().LOGIN["status"] == "idle" and runtime().LOGIN["message"] == "尚未登录":
            result.update(status="saved", message="已保存登录状态，采集时自动复用")
    return public_view.technical(result)


def accept_session(state, message, *, expected_generation=None):
    """One authority for uploads and Mac logins, independent of account role."""
    if not session_state.save(runtime().SESSION, state, expected_generation=expected_generation):
        return False
    session_verification.invalidate(runtime().SESSION)
    runtime().LOGIN.update(status="saved", message=message, issue=None, navigating=False)
    return True


async def cancel_login():
    task = runtime().login_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def import_session(state, message):
    # Do not hold this lock while a browser is waiting for a human. A valid
    # upload supersedes that older browser, closes it and becomes authoritative.
    async with runtime().SESSION_LOCK:
        await cancel_login()
        if runtime().SESSION.is_file():
            try:
                previous = json.loads(runtime().SESSION.read_text())
            except (OSError, ValueError):
                previous = None
            if previous is not None:
                backup = runtime().SESSION.with_name("taobao.json.previous")
                from crawler import save_json

                save_json(backup, previous)
                backup.chmod(0o600)
        accept_session(state, message)


def browser_error_reason(error):
    """Only expose an error code, never URLs/headers from a browser exception."""
    match = re.search(r"net::(ERR_[A-Z0-9_]+)", str(error))
    if match:
        return {
            "ERR_CONNECTION_CLOSED": "网络连接被关闭（ERR_CONNECTION_CLOSED）",
            "ERR_CONNECTION_RESET": "网络连接被重置（ERR_CONNECTION_RESET）",
            "ERR_PROXY_CONNECTION_FAILED": "无法连接浏览器代理（ERR_PROXY_CONNECTION_FAILED）",
            "ERR_INTERNET_DISCONNECTED": "当前网络已断开（ERR_INTERNET_DISCONNECTED）",
            "ERR_NAME_NOT_RESOLVED": "无法解析淘宝域名（ERR_NAME_NOT_RESOLVED）",
        }.get(match[1], match[1])
    return (
        "页面加载超时"
        if isinstance(error, asyncio.TimeoutError)
        or type(error).__name__ == "TimeoutError"
        else type(error).__name__
    )


async def _login_signal(closed, timeout):
    """Await UI/browser actions without leaking wait tasks on cancel or timeout."""
    waiters = {
        asyncio.create_task(closed.wait()): "closed",
        asyncio.create_task(runtime().login_done.wait()): "complete",
        asyncio.create_task(runtime().login_retry.wait()): "retry",
    }
    try:
        done, _ = await asyncio.wait(
            waiters, timeout=max(0, timeout), return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            return "timeout"
        # If the window was closed while Complete was clicked, do not overwrite
        # the last known session with an empty/closed browser context.
        return next(
            action
            for action in ("closed", "complete", "retry")
            if any(waiters[t] == action for t in done)
        )
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def login():
    """Keep the Mac login window alive through recoverable navigation/auth errors."""
    from playwright.async_api import Error as PlaywrightError, async_playwright

    login_generation = session_state.generation(runtime().SESSION)

    def update_login(**values):
        if session_state.generation(runtime().SESSION) == login_generation:
            runtime().LOGIN.update(**values)

    update_login(
        status="opening",
        message="正在打开本机浏览器",
        started_at=s.now(),
        issue=None,
        navigating=False,
    )
    runtime().login_done.clear()
    runtime().login_retry.clear()
    closed = asyncio.Event()
    stage = "launch"
    try:
        async with async_playwright() as p:
            browser_name = s.settings()["browser"]
            browser = await p.chromium.launch(
                headless=False,
                **taobao_browser_options(browser_name),
            )
            browser.on("disconnected", lambda *_: closed.set())
            try:
                stage = "context"
                try:
                    context = await browser.new_context(
                        **taobao_context_options(),
                        **(
                            {"storage_state": str(runtime().SESSION)}
                            if runtime().SESSION.exists()
                            else {}
                        ),
                    )
                except Exception:
                    if not runtime().SESSION.exists() or not browser.is_connected():
                        raise
                    context = await browser.new_context(**taobao_context_options())
                    s.log(
                        "旧会话无法载入，已打开全新登录窗口；原会话文件保留",
                        level="warning",
                    )
                context.on("close", lambda *_: closed.set())

                def watch_page(page):
                    def page_closed(*_):
                        if not any(not p.is_closed() for p in context.pages):
                            closed.set()

                    page.on("close", page_closed)

                context.on("page", watch_page)
                page = await context.new_page()
                deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_SECONDS

                async def navigate():
                    nonlocal page, stage
                    stage = "navigation"
                    update_login(navigating=True, issue=None)
                    try:
                        pages = [p for p in context.pages if not p.is_closed()]
                        if not pages:
                            closed.set()
                            return
                        page = pages[-1]
                        response = await page.goto(
                            LOGIN_URL, wait_until="domcontentloaded", timeout=45000
                        )
                    except PlaywrightError as exc:
                        if closed.is_set() or not browser.is_connected():
                            return
                        reason = browser_error_reason(exc)
                        update_login(
                            status="waiting",
                            issue="navigation",
                            message=f"登录页打开失败：{reason}。窗口已保留，可在浏览器刷新，或点击「重试打开登录页」。",
                        )
                        s.log(
                            f"淘宝登录页加载失败：{reason}；保留登录窗口",
                            level="warning",
                        )
                    else:
                        update_login(
                            status="waiting",
                            issue=None,
                            message="请在浏览器中登录，然后点击「完成登录并保存」",
                        )
                        s.log(
                            f"淘宝登录页已打开（HTTP {response.status if response else 'unknown'}）"
                        )
                    finally:
                        update_login(navigating=False)

                await navigate()
                while True:
                    signal = await _login_signal(
                        closed, deadline - asyncio.get_running_loop().time()
                    )
                    if signal in ("closed", "timeout"):
                        update_login(
                            status="idle",
                            issue=None,
                            message=(
                                "登录窗口已关闭，登录状态未更新"
                                if signal == "closed"
                                else "登录等待已超过 15 分钟，请重新打开登录"
                            ),
                            navigating=False,
                        )
                        break
                    if signal == "retry":
                        runtime().login_retry.clear()
                        await navigate()
                        continue
                    runtime().login_done.clear()
                    stage = "validation"
                    state = await context.storage_state(indexed_db=True)
                    auth_cookies = [
                        c
                        for c in state.get("cookies", [])
                        if c["name"] in ("_nk_", "unb", "lgc")
                        and c.get("value")
                        and c.get("domain", "").lstrip(".")
                        in ("taobao.com", "tmall.com", "login.taobao.com")
                    ]
                    if not auth_cookies:
                        update_login(
                            status="waiting",
                            issue="not_logged_in",
                            message="还未检测到淘宝登录状态。窗口已保留，请先完成登录，再点击「完成登录并保存」。",
                        )
                        continue
                    stage = "save"
                    async with runtime().SESSION_LOCK:
                        saved = accept_session(
                            state, "登录状态已保存，后续采集自动复用",
                            expected_generation=login_generation,
                        )
                    if saved:
                        s.log("淘宝登录状态已保存")
                    break
            finally:
                if browser.is_connected():
                    await browser.close()
    except asyncio.CancelledError:
        update_login(status="idle", message="已取消登录", issue=None, navigating=False)
        raise
    except Exception as exc:
        reason = browser_error_reason(exc)
        update_login(
            status="error",
            message=f"登录未完成：{reason}，请重试或切换本机浏览器。",
            issue=stage,
            navigating=False,
        )
        s.log(f"淘宝登录失败（{stage}）：{reason}", level="warning")


async def stop_process(process, tree, communicate_task=None):
    """Kill the crawler and tracked browsers, then reap pipe/process handles."""
    tree.kill()
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    if communicate_task is not None:
        # A detached browser can inherit stderr and keep the pipe open after
        # the crawler exits. Reusing communicate() here would race the monitor
        # task, so reap the parent and cancel only that monitor.
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, PermissionError):
                    pass
        communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)
        return
    try:
        await asyncio.wait_for(process.communicate(), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                process.kill()
            except (ProcessLookupError, PermissionError):
                pass


def timeout_result():
    result = {
        "price": None,
        "status": "timeout",
        "stock": "unknown",
        "diagnostics": {
            "stage": "subprocess",
            "timeout_seconds": COLLECT_TIMEOUT_SECONDS,
            "exception_message": f"采集超过 {COLLECT_TIMEOUT_SECONDS} 秒，已终止爬虫及浏览器子进程；本次不自动重试",
        },
    }
    result["diagnostics"] = describe(result)
    return result


def timeout_result_with_progress(progress):
    result = timeout_result()
    if isinstance(progress, dict):
        diagnostics = progress.get("diagnostics")
        if isinstance(diagnostics, dict):
            # Preserve the last useful stage and browser location while keeping
            # the public status as timeout when no login redirect was observed.
            for key in ("stage", "final_url", "http_status", "final_host", "title",
                        "document_ready_state", "body_text_length", "dom_element_count", "script_count",
                        "iframe_count", "render_state", "page_signals", "request_failures",
                        "resource_http_errors", "script_error_types", "page_diagnostics_unavailable", "price_nodes"):
                if diagnostics.get(key) is not None:
                    result["diagnostics"][key] = diagnostics[key]
        if progress.get("final_host"):
            result["diagnostics"]["final_host"] = progress["final_host"]
    result["diagnostics"] = describe(result)
    return result


def _read_progress(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _progress_result(progress):
    if not progress:
        return None
    verification = (progress.get("status") == "blocked"
                    and (progress.get("diagnostics") or {}).get("verification_required"))
    if progress.get("status") != "login_or_redirect" and not verification:
        return None
    result = {
        "price": None,
        "status": "blocked" if verification else "login_or_redirect",
        "stock": "unknown",
        "final_host": progress.get("final_host"),
        "diagnostics": dict(progress.get("diagnostics") or {}),
    }
    if verification:
        result["diagnostics"].update(
            stage="security_verification", login_detected=False,
            exception_message="淘宝要求人工安全验证，本次采集已停止，不自动重试",
        )
    else:
        result["diagnostics"].update(
            stage="login_redirect", login_detected=True,
            exception_message="浏览器检测到淘宝登录页，已提前终止本次采集",
        )
    result["diagnostics"] = describe(result)
    return result


async def collect(product):
    if urlsplit(product["url"]).hostname not in TAOBAO_HOSTS:
        result = {"price": None, "status": "source_unsupported", "stock": "unknown"}
        result["diagnostics"] = describe(result)
        return result
    settings = s.settings()
    process = None
    tree = None
    watcher = None
    deadline = asyncio.get_running_loop().time() + COLLECT_TIMEOUT_SECONDS
    try:
        # Each process can read/update its own Playwright state without holding
        # the interactive login lock for the duration of a crawl.
        with tempfile.TemporaryDirectory(prefix=".crawl-", dir=s.data_dir()) as folder:
            job_file = Path(folder) / "result.json"
            state_file = Path(folder) / "session.json"
            progress_file = Path(folder) / "progress.json"
            generation, baseline, refresh_order = session_state.refresh_snapshot(runtime().SESSION)
            if s.TENANT.get() != "admin" and not (baseline and baseline.get("cookies")):
                result = {
                    "price": None, "status": "login_or_redirect", "stock": "unknown",
                    "diagnostics": {"stage": "session_state", "cookie_required": True,
                        "session_state_loaded": False, "loaded_cookie_count": 0,
                        "product_cookie_count": 0},
                }
                result["diagnostics"] = describe(result)
                return result
            if baseline is not None:
                from crawler import save_json

                save_json(state_file, baseline)
                state_file.chmod(0o600)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(s.ROOT / "crawler.py"),
                product["url"],
                "--browser",
                settings["browser"],
                *(["--headed"] if settings["crawler_headed"] else []),
                "--wait",
                str(settings["wait_seconds"]),
                "--timeout",
                str(COLLECT_TIMEOUT_SECONDS),
                "--state",
                str(state_file),
                "--output",
                str(job_file),
                "--progress",
                str(progress_file),
                cwd=str(s.ROOT),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            tree = CrawlerProcessTree(process.pid)
            watcher = asyncio.create_task(tree.watch())
            communicate_task = asyncio.create_task(process.communicate())
            early_result = None
            try:
                while not communicate_task.done():
                    early_result = _progress_result(_read_progress(progress_file))
                    if early_result:
                        await stop_process(process, tree, communicate_task)
                        process = None
                        return early_result
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        snapshot = _read_progress(progress_file)
                        early_result = _progress_result(snapshot)
                        await stop_process(process, tree, communicate_task)
                        process = None
                        return early_result or timeout_result_with_progress(snapshot)
                    try:
                        await asyncio.wait_for(asyncio.shield(communicate_task), timeout=min(0.2, remaining))
                    except asyncio.TimeoutError:
                        continue
                _, stderr = communicate_task.result()
            except asyncio.CancelledError:
                await stop_process(process, tree, communicate_task)
                process = None
                raise
            if not job_file.exists():
                text = (stderr or b"").decode("utf-8", errors="replace")
                detail = exception_detail(RuntimeError(text), "subprocess")
                kind = re.search(
                    r"\b(ModuleNotFoundError|ImportError|SyntaxError|PermissionError|FileNotFoundError)\b",
                    text,
                )
                if kind:
                    detail["error_type"] = kind.group(1)
                result = {
                    "price": None,
                    "status": "crawler_error",
                    "stock": "unknown",
                    "diagnostics": {
                        **detail,
                        "exit_code": process.returncode,
                        "exception_message": detail.get("exception_message")
                        or "采集进程退出但未生成结果文件，请检查依赖与运行环境",
                    },
                }
                result["diagnostics"] = describe(result)
                return result
            result = json.loads(job_file.read_text())
            if (
                result.get("session_state_saved")
                and state_file.is_file()
                and result.get("status") not in ("login_or_redirect", "blocked")
            ):
                session_state.merge(
                    runtime().SESSION, generation, baseline, json.loads(state_file.read_text()),
                    order=refresh_order,
                )
        result = finalize_result(result, product)
        if (result.get("status") == "login_or_redirect"
                and session_state.generation(runtime().SESSION) == generation
                and runtime().LOGIN["status"] not in ("opening", "waiting")):
            if runtime().LOGIN["status"] != "expired":
                s.system_notice("鬼打架 · 淘宝登录状态需要更新，请在设置中重新登录。")
            runtime().LOGIN.update(status="expired", message="登录状态已失效，请重新登录")
        return {
            **result,
            "original_price": cents(result.get("original_price")),
        }
    except asyncio.TimeoutError:
        return timeout_result()
    except Exception as exc:
        result = {
            "price": None,
            "status": "crawler_error",
            "error_type": type(exc).__name__,
            "stock": "unknown",
            "diagnostics": exception_detail(exc, "subprocess"),
        }
        result["diagnostics"] = describe(result)
        return result
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if process is not None and not (tree is not None and getattr(tree, "closed", False)):
            await stop_process(process, tree)


def finalize_result(result, product):
    result = {**result}
    result["diagnostics"] = describe(result)
    price = cents(result.get("price"))
    expected = product.get("expected_options", [])
    selected = " ".join(result.get("selected_options", []))
    explicit_mismatch = (
        bool(result.get("requested_sku_id") and result.get("selected_sku_id"))
        and result["selected_sku_id"] != result["requested_sku_id"]
    )
    verified = bool(result.get("sku_verified")) or (
        not explicit_mismatch
        and bool(expected)
        and all(option in selected for option in expected)
    )
    # Preserve the real failure (navigation, missing DOM, masked amount, etc.).
    # SKU rejection only applies after a complete price has actually been read.
    if (
        result.get("requested_sku_id")
        and result.get("status") == "success"
        and price is not None
    ):
        if explicit_mismatch:
            result["status"] = "sku_mismatch"
            price = None
        elif not verified:
            result["status"] = "sku_unverified"
            price = None
        elif expected and not result.get("sku_verified"):
            result["sku_verification"] = "expected_options"
        if price is None:
            result["diagnostics"]["stage"] = "sku_verification"
    if result.get("status") != "success":
        price = None
    result.update(price=price, sku_verified=verified, expected_options=expected)
    result["diagnostics"] = describe(result)
    return result
