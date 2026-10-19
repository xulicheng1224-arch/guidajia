import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import access, accounts, app as server, store as s
from tests.test_system import client, TEST_PASSWORD

ORIGIN = "https://monitor.example.com"


@pytest.fixture
def remote(client):
    (s.DATA / "public-access.json").write_text(json.dumps({"origin": ORIGIN}))
    with TestClient(server.app, base_url=ORIGIN) as browser:
        yield browser


def login(remote, password=TEST_PASSWORD, **headers):
    return remote.post(
        "/api/access/login",
        json={"email": accounts.ADMIN_EMAIL, "password": password},
        headers={"Origin": ORIGIN, **headers},
    )


def test_public_access_disabled_by_default(client):
    assert client.get("/", headers={"Host": "monitor.example.com"}).status_code == 403


def test_public_login_gate_never_bootstraps_owner(remote):
    assert (
        remote.get(
            "/",
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        ).status_code
        == 200
    )
    for path in ("/", "/settings?tab=account", "/products"):
        response = remote.get(path)
        assert response.status_code == 200
        assert "登录你的工作空间" in response.text
        assert "test-secret" not in response.text
        assert "no-store" in response.headers["cache-control"]
        assert "nonce-" in response.headers["content-security-policy"]
    for path in (
        "/api/bootstrap",
        "/api/state",
        "/api/docs",
        "/openapi.json",
        "/api/stream",
    ):
        response = remote.get(path)
        assert response.status_code == 401
        assert response.json()["code"] == "login_required"
        assert "set-cookie" not in response.headers
    assert not remote.cookies


def test_public_brand_image_loads_before_login_without_exposing_other_files(remote):
    from hashlib import sha256

    page = remote.get("/")
    assert "登录 · 鬼打架" in page.text
    assert "img-src 'self'" in page.headers["content-security-policy"]
    image = remote.get("/brand/guidajia.jpg")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/jpeg")
    assert (
        sha256(image.content).digest()
        == sha256((s.ROOT / "public" / "brand" / "guidajia.jpg").read_bytes()).digest()
    )
    assert remote.head("/brand/guidajia.jpg").status_code == 200
    assert remote.get("/api/state").status_code == 401
    assert remote.get("/api/security/token").status_code == 401
    assert "登录你的工作空间" in remote.get("/brand/private.json").text
    assert (
        remote.get("/brand/guidajia.jpg", headers={"Host": "evil.example"}).status_code
        == 403
    )


def test_login_secure_session_and_logout(remote):
    assert login(remote, "中文错误口令").status_code == 401
    response = login(remote)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert all(
        flag in cookie
        for flag in ("Secure", "HttpOnly", "SameSite=strict", "Max-Age=43200")
    )
    assert "test-secret" not in cookie
    status = remote.get("/api/state").json()["access"]
    assert status["role"] == "admin" and status["can_write"]
    assert status["user"]["email"] == accounts.ADMIN_EMAIL
    response = remote.get("/api/bootstrap")
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert remote.get("/api/security/token").status_code == 410
    assert remote.post("/api/security/token").status_code == 410
    assert (
        remote.post("/api/access/logout", headers={"Origin": ORIGIN}).status_code == 200
    )
    assert remote.get("/api/state").status_code == 401


def test_unknown_host_local_rewrite_and_csrf_rejected(remote, client):
    assert remote.get("/", headers={"Host": "evil.example"}).status_code == 403
    for header in (
        "CF-Ray",
        "CF-Connecting-IP",
        "X-Forwarded-Host",
        "X-Forwarded-For",
        "X-Forwarded-Proto",
        "Forwarded",
    ):
        assert (
            client.get(
                "/api/bootstrap", headers={"Host": "localhost:8765", header: "x"}
            ).status_code
            == 403
        )
    assert (
        remote.post("/api/access/login", json={"email": accounts.ADMIN_EMAIL, "password": TEST_PASSWORD}).status_code
        == 403
    )
    assert login(remote, Origin="https://evil.example").status_code == 403
    assert login(remote).status_code == 200
    assert (
        remote.post(
            "/api/access/logout", headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        remote.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )


def test_raw_local_cookie_cannot_log_in_remotely(remote):
    remote.cookies.set("monitor_session", "test-secret")
    assert remote.get("/api/state").status_code == 401
    assert (
        remote.get(
            "/api/state", headers={"Authorization": "Bearer test-secret"}
        ).status_code
        == 401
    )


def test_expired_and_tampered_account_sessions(remote):
    assert login(remote).status_code == 200
    good = remote.cookies.get(accounts.COOKIE)
    remote.cookies.clear()
    remote.cookies.set(accounts.COOKIE, good[:-1] + ("A" if good[-1] != "A" else "B"))
    assert remote.get("/api/state").status_code == 401
    remote.cookies.clear()
    remote.cookies.set(accounts.COOKIE, good)
    with accounts.connection() as db:
        db.execute("UPDATE sessions SET expires_at=0")
    assert remote.get("/api/state").status_code == 401


def test_password_change_invalidates_public_sessions(remote, client):
    assert login(remote).status_code == 200
    assert client.post("/api/account/password", json={"current_password": TEST_PASSWORD, "password": "ChangedAdmin123"}).status_code == 200
    assert remote.get("/api/state").status_code == 401
    with accounts.connection() as db:
        db.execute("DELETE FROM limits")
    assert login(remote, "ChangedAdmin123").status_code == 200


def test_rate_limit_and_body_size(remote):
    for _ in range(10):
        assert login(remote, "wrong").status_code == 401
    response = login(remote)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"
    with accounts.connection() as db:
        db.execute("DELETE FROM limits")
    assert login(remote).status_code == 200
    assert (
        remote.post(
            "/api/access/login", headers={"Origin": ORIGIN}, content="x" * 9000
        ).status_code
        == 413
    )


def test_http_navigation_upgrades_to_canonical_https_preserving_path(remote):
    response = remote.get(
        "/products?group=all",
        headers={"CF-Visitor": '{"scheme":"http"}'},
        follow_redirects=False,
    )
    assert response.status_code == 308
    assert response.headers["location"] == ORIGIN + "/products?group=all"
    assert "set-cookie" not in response.headers
    # The edge scheme takes priority over the internal request/forwarded scheme.
    assert (
        remote.get(
            "/",
            headers={"CF-Visitor": '{"scheme":"http"}', "X-Forwarded-Proto": "https"},
            follow_redirects=False,
        ).status_code
        == 308
    )


def test_http_login_returns_upgrade_instead_of_cross_site_or_post_redirect(remote):
    response = remote.post(
        "/api/access/login",
        json={"email": accounts.ADMIN_EMAIL, "password": TEST_PASSWORD},
        headers={
            "Origin": ORIGIN.replace("https://", "http://"),
            "CF-Visitor": '{"scheme":"http"}',
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "https_required"
    assert response.json()["redirect"] == ORIGIN + "/"
    assert "set-cookie" not in response.headers
    assert "test-secret" not in response.text
    # A stale HTTP page must upgrade even if the edge has redirected its fetch.
    response = remote.post(
        "/api/access/login",
        json={"email": accounts.ADMIN_EMAIL, "password": TEST_PASSWORD},
        headers={"Origin": ORIGIN.replace("https://", "http://")},
    )
    assert response.json()["code"] == "https_required"


def test_https_tunnel_over_http_has_no_redirect_loop_and_retains_csrf(client):
    (s.DATA / "public-access.json").write_text(json.dumps({"origin": ORIGIN}))
    headers = {"CF-Visitor": '{"scheme":"https"}', "X-Forwarded-Proto": "http"}
    with TestClient(
        server.app, base_url=ORIGIN.replace("https://", "http://")
    ) as tunnel:
        response = tunnel.get("/products", headers=headers, follow_redirects=False)
        assert response.status_code == 200 and "登录你的工作空间" in response.text
        assert (
            tunnel.post(
                "/api/access/login",
                json={"email": accounts.ADMIN_EMAIL, "password": TEST_PASSWORD},
                headers={**headers, "Origin": ORIGIN},
            ).status_code
            == 200
        )
        # HTTPS edge hints cannot authorize an unrelated cross-origin login.
        assert (
            tunnel.post(
                "/api/access/login",
                json={"email": accounts.ADMIN_EMAIL, "password": TEST_PASSWORD},
                headers={**headers, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            tunnel.get(
                "/api/bootstrap", headers={**headers, "Host": "evil.example"}
            ).status_code
            == 403
        )


def test_http_fallback_and_local_http_behavior(client):
    (s.DATA / "public-access.json").write_text(json.dumps({"origin": ORIGIN}))
    with TestClient(
        server.app, base_url=ORIGIN.replace("https://", "http://")
    ) as tunnel:
        assert tunnel.get("/", follow_redirects=False).status_code == 308
        for headers in (
            {"X-Forwarded-Proto": "https"},
            {"CF-Visitor": "invalid-json", "X-Forwarded-Proto": "https"},
        ):
            assert (
                tunnel.get("/", headers=headers, follow_redirects=False).status_code
                == 200
            )
    assert client.get("/api/bootstrap").status_code == 200
    assert client.get("/api/state").json()["access"]["role"] == "admin"


@pytest.mark.parametrize(
    "origin",
    [
        "http://monitor.example.com",
        "https://monitor.example.com/",
        "https://monitor.example.com@evil.example",
        "https://monitor.example.com:123",
        "https://monitor.example.com?x=1",
        None,
        {},
    ],
)
def test_invalid_origin_config_fails_closed(client, origin):
    (s.DATA / "public-access.json").write_text(json.dumps({"origin": origin}))
    assert client.get("/", headers={"Host": "monitor.example.com"}).status_code == 403
