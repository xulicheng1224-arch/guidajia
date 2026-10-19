"""Regression coverage for login window lifetime, retry and session preservation."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import playwright.async_api
from playwright.async_api import Error as BrowserError

from backend import collector, store as s
from browser_config import taobao_browser_options
from tests.test_system import client


class Emitter:
    def __init__(self):
        self.events = {}

    def on(self, event, callback):
        self.events.setdefault(event, []).append(callback)

    def emit(self, event, *args):
        for callback in self.events.get(event, []):
            callback(*args)


class Page(Emitter):
    def __init__(self, context):
        super().__init__()
        self.context = context
        self.closed = False
        self.navigation_errors = []
        self.navigations = 0

    def is_closed(self):
        return self.closed

    async def goto(self, *args, **kwargs):
        self.navigations += 1
        error = self.navigation_errors.pop(0) if self.navigation_errors else None
        if error:
            raise error
        return SimpleNamespace(status=200)

    async def close(self):
        self.closed = True
        self.context.pages.remove(self)
        self.emit("close")


class Context(Emitter):
    def __init__(self):
        super().__init__()
        self.pages = []
        self.state = {"cookies": [], "origins": []}
        self.initial_errors = []

    async def new_page(self):
        page = Page(self)
        page.navigation_errors = self.initial_errors
        self.pages.append(page)
        self.emit("page", page)
        return page

    async def storage_state(self, **kwargs):
        assert kwargs["indexed_db"] is True
        return self.state


class Browser(Emitter):
    def __init__(self):
        super().__init__()
        self.context = Context()
        self.connected = True
        self.close_count = 0
        self.context_count = 0
        self.bad_saved_state = False
        self.options = None

    def is_connected(self):
        return self.connected

    async def new_context(self, **kwargs):
        self.context_count += 1
        if self.bad_saved_state and "storage_state" in kwargs:
            raise BrowserError("Invalid storage state")
        return self.context

    async def close(self):
        self.close_count += 1
        self.connected = False
        self.emit("disconnected")


class Playwright:
    def __init__(self, browser):
        self.browser = browser
        self.chromium = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def launch(self, **kwargs):
        self.browser.options = kwargs
        return self.browser


@pytest.fixture
def fake_browser(client, monkeypatch):
    browser = Browser()
    monkeypatch.setattr(
        playwright.async_api, "async_playwright", lambda: Playwright(browser)
    )
    monkeypatch.setattr(collector, "login_done", asyncio.Event())
    monkeypatch.setattr(collector, "login_retry", asyncio.Event())
    monkeypatch.setattr(collector, "SESSION_LOCK", asyncio.Lock())
    return browser


async def until(predicate):
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Login did not reach expected state: {collector.LOGIN}")


async def cancel(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_network_error_preserves_window_and_retry_reuses_it(fake_browser):
    async def exercise():
        fake_browser.context.initial_errors = [
            BrowserError(
                "Page.goto: net::ERR_CONNECTION_CLOSED at https://login.taobao.com/?secret=hidden"
            )
        ]
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        assert fake_browser.connected and fake_browser.close_count == 0
        assert collector.LOGIN["issue"] == "navigation"
        assert "ERR_CONNECTION_CLOSED" in collector.LOGIN["message"]
        assert "secret" not in collector.LOGIN["message"]
        collector.login_retry.set()
        await until(lambda: fake_browser.context.pages[0].navigations == 2)
        assert collector.LOGIN["issue"] is None
        assert fake_browser.context_count == 1
        assert fake_browser.close_count == 0
        await cancel(task)
        assert fake_browser.close_count == 1

    asyncio.run(exercise())


def test_complete_before_login_keeps_window_and_existing_session(fake_browser):
    original = {
        "cookies": [
            {"name": "unb", "value": "old-test-value", "domain": ".taobao.com"}
        ],
        "origins": [],
    }
    collector.SESSION.write_text(json.dumps(original))

    async def exercise():
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        collector.login_done.set()
        await until(lambda: collector.LOGIN["issue"] == "not_logged_in")
        assert fake_browser.connected
        assert json.loads(collector.SESSION.read_text()) == original
        fake_browser.context.state = {
            "cookies": [
                {"name": "unb", "value": "new-test-value", "domain": ".taobao.com"}
            ],
            "origins": [],
        }
        collector.login_done.set()
        await asyncio.wait_for(task, 1)
        assert collector.LOGIN["status"] == "saved"
        assert json.loads(collector.SESSION.read_text()) == fake_browser.context.state
        assert collector.SESSION.stat().st_mode & 0o777 == 0o600
        assert fake_browser.close_count == 1

    asyncio.run(exercise())


def test_user_closes_last_page_ends_login_immediately(fake_browser):
    async def exercise():
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        await fake_browser.context.pages[0].close()
        await asyncio.wait_for(task, 1)
        assert collector.LOGIN["status"] == "idle"
        assert "已关闭" in collector.LOGIN["message"]
        assert not collector.SESSION.exists()
        assert fake_browser.close_count == 1

    asyncio.run(exercise())


def test_cancel_cleans_up_wait_tasks_and_browser(fake_browser):
    async def exercise():
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        await cancel(task)
        assert collector.LOGIN["message"] == "已取消登录"
        assert fake_browser.close_count == 1
        assert not [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]

    asyncio.run(exercise())


def test_bad_saved_state_falls_back_without_destroying_old_file(fake_browser):
    collector.SESSION.write_text("broken saved state")
    fake_browser.bad_saved_state = True

    async def exercise():
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        assert fake_browser.context_count == 2
        assert fake_browser.close_count == 0
        assert collector.SESSION.read_text() == "broken saved state"
        await cancel(task)

    asyncio.run(exercise())


def test_unrelated_domain_cookie_cannot_confirm_taobao_login(fake_browser):
    async def exercise():
        fake_browser.context.state = {
            "cookies": [{"name": "unb", "value": "unrelated", "domain": "example.com"}]
        }
        task = asyncio.create_task(collector.login())
        await until(lambda: collector.LOGIN["status"] == "waiting")
        collector.login_done.set()
        await until(lambda: collector.LOGIN["issue"] == "not_logged_in")
        assert not collector.SESSION.exists()
        await cancel(task)

    asyncio.run(exercise())


def test_login_timeout_closes_window_cleanly(fake_browser, monkeypatch):
    monkeypatch.setattr(collector, "LOGIN_TIMEOUT_SECONDS", 0.01)
    asyncio.run(asyncio.wait_for(collector.login(), 1))
    assert collector.LOGIN["status"] == "idle"
    assert "超过" in collector.LOGIN["message"]
    assert fake_browser.close_count == 1


def test_taobao_browser_does_not_inherit_terminal_proxies(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:password@127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("MONITOR_TEST_VALUE", "preserved")
    options = taobao_browser_options("msedge")
    assert options["channel"] == "msedge"
    assert options["args"] == ["--no-proxy-server"]
    assert not any(
        key.lower() in ("http_proxy", "https_proxy", "all_proxy")
        for key in options["env"]
    )
    assert options["env"]["MONITOR_TEST_VALUE"] == "preserved"
    assert "channel" not in taobao_browser_options("chromium")


def test_retry_without_active_login_is_reported(client):
    assert client.post("/api/session/retry", json={}).status_code == 409
