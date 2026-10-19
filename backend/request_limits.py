"""Bound ASGI bodies before FastAPI/JSON allocates or parses their content."""

import asyncio

from starlette.responses import JSONResponse

DEFAULT_LIMIT = 128 * 1024
IMPORT_LIMIT = 12 * 1024 * 1024
ACCOUNT_LIMIT = 8192
BODY_TIMEOUT = 30


def body_limit(path):
    if path.startswith(("/api/access/", "/api/account/", "/api/security/")):
        return ACCOUNT_LIMIT
    if path == "/api/session/import" or path.startswith("/api/import/"):
        return IMPORT_LIMIT
    return DEFAULT_LIMIT


class RequestLimitsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = body_limit(scope.get("path", ""))

        async def reject(status, message):
            response = JSONResponse({"detail": message}, status_code=status, headers={
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"
            })
            await response(scope, receive, send)

        lengths = [value for key, value in scope.get("headers", ()) if key.lower() == b"content-length"]
        if lengths:
            try:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError()
                if int(lengths[0]) > limit:
                    return await reject(413, "请求内容过大，请缩小内容后重试")
            except ValueError:
                return await reject(400, "请求长度无效")
        body = bytearray()
        try:
            async with asyncio.timeout(BODY_TIMEOUT):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > limit:
                        return await reject(413, "请求内容过大，请缩小内容后重试")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "上传请求超时，请重试")
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
