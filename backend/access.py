"""Same-origin account access for localhost and the configured Tunnel domain."""

import ipaddress
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import store as s

REMOTE_COOKIE = "monitor_remote_session"


def public_origin():
    try:
        value = json.loads((s.DATA / "public-access.json").read_text())["origin"]
        if not isinstance(value, str):
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.netloc == parsed.hostname
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        ):
            return value
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def is_local(request: Request):
    host = request.headers.get("host", "").lower()
    try:
        name = urlsplit("//" + host).hostname
        peer = request.client.host if request.client else ""
        if name == "testserver" and peer == "testclient":
            return os.environ.get("MONITOR_DISABLE_WORKER") == "1"
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False
    # A Tunnel must retain the public Host; never bootstrap an owner session
    # for a forwarded request rewritten to localhost.
    forwarded = any(
        header in request.headers
        for header in (
            "cf-ray",
            "cf-connecting-ip",
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
        )
    )
    return name in ("localhost", "127.0.0.1", "::1") and loopback and not forwarded


def status(request):
    origin = public_origin()
    role = getattr(request.state, "role", None)
    return {
        "enabled": bool(origin),
        "origin": origin,
        "remote": not is_local(request),
        "role": role,
        "read_only": role == "viewer",
        "can_write": role in ("admin", "user"),
        "user": getattr(request.state, "user", None),
    }


def visitor_scheme(request):
    """TLS ends at Cloudflare, so the Tunnel's HTTP hop is not the visitor URL.

    These headers only decide whether to upgrade transport. They never grant
    authentication, relax Origin checks, or choose the destination host.
    """
    try:
        visitor = json.loads(request.headers.get("cf-visitor", "{}"))
        if isinstance(visitor, dict) and visitor.get("scheme") in ("http", "https"):
            return visitor["scheme"]
    except (TypeError, ValueError):
        pass
    forwarded = request.headers.get("x-forwarded-proto", "").strip().lower()
    return forwarded if forwarded in ("http", "https") else request.url.scheme


def unauthorized(request):
    if (
        request.method == "GET"
        and not request.url.path.startswith("/api/")
        and request.url.path != "/openapi.json"
    ):
        nonce = secrets.token_urlsafe(24)
        page = (
            (Path(__file__).with_name("login.html"))
            .read_text()
            .replace("__NONCE__", nonce)
        )
        response = HTMLResponse(page)
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response
    return JSONResponse(
        {"detail": "请登录后访问", "code": "login_required"}, status_code=401
    )


def guard(request, token=None):
    from . import accounts
    local = is_local(request)
    origin = public_origin()
    public_host = urlsplit(origin).netloc if origin else None
    remote = not local
    request.state.remote = remote
    request.state.role = None
    request.state.user = None
    if remote and request.headers.get("host", "").lower() != public_host:
        return JSONResponse({"detail": "未允许的访问域名"}, status_code=403)
    source = request.headers.get("origin")
    if remote and (visitor_scheme(request) == "http" or source == origin.replace("https://", "http://", 1)):
        if request.method in ("GET", "HEAD"):
            target = origin + request.url.path
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(target, status_code=308)
        return JSONResponse({"detail": "请使用 HTTPS 地址访问并重新登录", "code": "https_required", "redirect": origin + "/"}, status_code=403)
    allowed = {origin} if remote else {
        f"{request.url.scheme}://{request.headers.get('host', '')}",
        "http://localhost:8765", "http://127.0.0.1:8765", "http://localhost:5173",
        "http://127.0.0.1:5173", "http://testserver",
    }
    cross_site = request.headers.get("sec-fetch-site") == "cross-site"
    navigation = request.method == "GET" and request.headers.get("sec-fetch-mode") == "navigate" and request.headers.get("sec-fetch-dest") == "document"
    if (source and source not in allowed) or (cross_site and not navigation):
        return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
    path = request.url.path.rstrip("/")
    if request.method in ("GET", "HEAD") and path in ("/brand/guidajia.jpg", "/api/ping", "/api/access/options"):
        return None
    if request.method == "POST" and path in (
        "/api/access/login", "/api/access/register",
    ):
        if remote and source != origin:
            return JSONResponse({"detail": "登录请求来源无效"}, status_code=403)
        return None
    # Legacy Bearer credentials and old cookies never grant account access.
    user = None if request.headers.get("authorization") is not None else accounts.session_user(request.cookies.get(accounts.COOKIE))
    if not user:
        return unauthorized(request)
    request.state.user = user
    request.state.role = user["role"]
    if path in ("/api/docs", "/docs", "/redoc", "/openapi.json") and user["role"] != "admin":
        return JSONResponse({"detail": "只有管理员可以访问接口文档"}, status_code=403)
    if path.startswith("/api/security") and path not in ("/api/security/token", "/api/security/read-only-token") and user["role"] != "admin":
        return JSONResponse({"detail": "只有管理员可以执行此操作"}, status_code=403)
    if path in ("/api/security/token", "/api/security/read-only-token"):
        return JSONResponse({"detail": "令牌登录已停用，请使用邮箱账号登录"}, status_code=410)
    if path in ("/api/session/login", "/api/session/complete", "/api/session/retry", "/api/session/cancel") and user["role"] != "admin":
        return JSONResponse({"detail": "请在本设备登录淘宝并上传自己的 Cookie 文件"}, status_code=403)
    return None


