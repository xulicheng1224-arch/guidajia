"""Owner-authorized security regressions; no external or private network calls."""

import asyncio
import csv
import io
import socket
from email.message import EmailMessage

import httpx
import pytest

from backend import csv_io, outbound, worker, request_limits
from tests.test_system import client


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/test", "https://[::1]/test",
    "https://169.254.169.254/latest", "https://10.0.0.1/secret",
    "https://localhost/test", "https://user:secret@push.example/test",
])
def test_webhook_private_targets_rejected(client, url):
    response = client.patch("/api/settings", json={"channels": {"bark": {"url": url, "enabled": True}}})
    assert response.status_code == 422


def test_request_rejected_before_json_parsing(client):
    response = client.patch("/api/settings", content=b" " * (128 * 1024 + 1),
                            headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_import_has_separate_limit_and_accounts_remain_small(client):
    content = b" " * (128 * 1024) + b'{"text":"name\\nSecurity fixture"}'
    response = client.post("/api/import/product", content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert client.post("/api/account/password", content=b" " * 8193).status_code == 413


@pytest.mark.parametrize("value", ["=1+1", "\t=1+1", "\r=1+1", "\n@SUM(1)", "  +1+1", "\ufeff=1+1", "'quoted"])
def test_csv_formula_guard_and_roundtrip(value):
    text = csv_io.render("product", [{"name": value}])
    cell = next(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))["name"]
    assert cell.startswith("'")
    assert csv_io.parse("product", text)[0]["name"] == value


@pytest.mark.parametrize("address", ["0.0.0.0", "127.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1",
    "224.0.0.1", "::", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "2002:7f00:1::", "64:ff9b::7f00:1"])
def test_reserved_and_transition_ips_rejected(address):
    with pytest.raises(ValueError):
        outbound.public_ip(address)


@pytest.mark.parametrize("answers", [["127.0.0.1"], ["93.184.216.34", "10.0.0.1"], ["::1"]])
def test_dns_private_or_mixed_answers_rejected(answers):
    async def run():
        loop = asyncio.get_running_loop()
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in answers]
        original = loop.getaddrinfo
        loop.getaddrinfo = resolve
        try:
            with pytest.raises(ValueError, match="公网地址"):
                await outbound.resolve_public("push.example", 443)
        finally:
            loop.getaddrinfo = original
    asyncio.run(run())


def test_dns_pinning_keeps_tls_name_and_host(monkeypatch):
    seen = []
    async def handle(self, request):
        seen.append((request.url.host, request.headers["host"], request.extensions["sni_hostname"]))
        return httpx.Response(200, json={"code": 0})
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    async def run():
        async with outbound.PinnedTransport("push.example", "93.184.216.34") as transport:
            await transport.handle_async_request(httpx.Request("POST", "https://push.example/key"))
            with pytest.raises(ValueError):
                await transport.handle_async_request(httpx.Request("POST", "https://other.example/key"))
    asyncio.run(run())
    assert seen == [("93.184.216.34", "push.example", "push.example")]


@pytest.mark.parametrize("status,body,error", [
    (200, b'{"code":0}', None), (302, b"", httpx.HTTPStatusError),
    (200, b"x" * (outbound.MAX_RESPONSE + 1), ValueError),
    (200, b"[]", ValueError), (200, b"bad", ValueError),
])
def test_webhook_response_limits_and_no_redirect_or_proxy(monkeypatch, status, body, error):
    calls = []
    async def resolve(*args):
        return "93.184.216.34"
    monkeypatch.setattr(outbound, "resolve_public", resolve)
    real_client = httpx.AsyncClient
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body
    def client_factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        def handle(request):
            assert request.headers["accept-encoding"] == "identity"
            calls.append(request.url.host)
            return httpx.Response(status, stream=Body(), headers={"Location": "https://127.0.0.1/"})
        return real_client(transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False)
    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    if error:
        with pytest.raises(error):
            asyncio.run(outbound.post_json("https://push.example/key", json={"body": "test"}))
    else:
        assert asyncio.run(outbound.post_json("https://push.example/key", json={"body": "test"})) == {"code": 0}
    assert calls == ["push.example"]


def test_preexisting_unsafe_config_cannot_send(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Unsafe saved configuration reached the network")
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    with pytest.raises(ValueError):
        asyncio.run(worker.send("bark", "test", {"channels": {"bark": {"enabled": True, "url": "https://127.0.0.1/"}}}))


@pytest.mark.parametrize("host,port", [("127.0.0.1", 465), ("smtp.example", 25), ("smtp.example", 22), ("localhost", 587)])
def test_smtp_private_hosts_and_non_tls_ports_rejected(client, host, port):
    response = client.patch("/api/settings", json={"channels": {"email": {"enabled": True, "host": host, "port": port}}})
    assert response.status_code == 422


@pytest.mark.parametrize("port", [465, 587])
def test_smtp_pins_ip_and_verifies_hostname(monkeypatch, port):
    seen = []
    class Socket:
        def close(self):
            pass
    class TLS:
        def wrap_socket(self, sock, *, server_hostname):
            seen.append(("tls", server_hostname))
            return sock
    class SMTP(outbound.PinnedSMTP):
        def connect(self, host, port):
            self._host = host
            self.sock = self._get_socket(host, port, self.timeout)
            return 220, b"fixture"
        def starttls(self, *, context):
            self.sock = context.wrap_socket(self.sock, server_hostname=self._host)
        def login(self, user, password):
            seen.append(("login", user))
        def send_message(self, message):
            seen.append(("sent", True))
    # Preserve the real superclass for the SSL method's super() lookup.
    original_ssl = outbound.PinnedSMTPSSL
    class SSL(original_ssl, SMTP):
        pass
    def connect(address, timeout, source_address):
        seen.append(("tcp", address))
        return Socket()
    async def resolve(*args):
        return "93.184.216.34"
    monkeypatch.setattr(outbound, "resolve_public", resolve)
    monkeypatch.setattr(outbound.socket, "create_connection", connect)
    monkeypatch.setattr(outbound.ssl, "create_default_context", TLS)
    monkeypatch.setattr(outbound, "PinnedSMTP", SMTP)
    monkeypatch.setattr(outbound, "PinnedSMTPSSL", SSL)
    asyncio.run(outbound.send_email({"host": "smtp.example", "port": port, "user": "fixture", "password": "fixture"}, EmailMessage()))
    assert seen == [("tcp", ("93.184.216.34", port)), ("tls", "smtp.example"), ("login", "fixture"), ("sent", True)]


def test_chunked_body_limit_without_content_length():
    reached = []
    async def target(scope, receive, send):
        reached.append(True)
    async def run():
        messages = iter([{"type": "http.request", "body": b"x" * 70000, "more_body": True},
                         {"type": "http.request", "body": b"x" * 70000, "more_body": False}])
        sent = []
        async def receive():
            return next(messages)
        async def send(message):
            sent.append(message)
        await request_limits.RequestLimitsMiddleware(target)({"type": "http", "path": "/api/settings", "headers": []}, receive, send)
        assert sent[0]["status"] == 413
    asyncio.run(run())
    assert not reached


def test_authenticated_page_csp_and_login_nonce(client):
    page = client.get("/")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    for term in ("script-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "connect-src 'self'"):
        assert term in csp
    assert "unsafe-eval" not in csp
    client.cookies.clear()
    assert "nonce-" in client.get("/").headers["content-security-policy"]


def test_notification_test_rate_limit_is_per_account(client, monkeypatch):
    async def send(*args):
        pass
    monkeypatch.setattr(worker, "send", send)
    for _ in range(10):
        assert client.post("/api/channels/bark/test", json={}).status_code == 200
    response = client.post("/api/channels/bark/test", json={})
    assert response.status_code == 429 and response.headers["retry-after"] == "60"
