"""Restricted notification egress with DNS pinning and verified TLS."""

import asyncio
import ipaddress
import json
import re
import smtplib
import socket
import ssl
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE = 64 * 1024
TARGET_ERROR = "通知地址必须为公网地址，不能访问本机、内网或保留地址"


def public_ip(value):
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or address.is_reserved
            or getattr(address, "ipv4_mapped", None)
            or getattr(address, "sixtofour", None)
            or getattr(address, "teredo", None)):
        raise ValueError(TARGET_ERROR)
    if address.version == 6 and any(address in ipaddress.ip_network(network) for network in ("64:ff9b::/96", "64:ff9b:1::/48")):
        raise ValueError(TARGET_ERROR)
    return str(address)


def hostname(value):
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("通知服务器地址无效")
    if any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in "%/@\\?#"):
        raise ValueError("通知服务器地址无效")
    value = value.rstrip(".").lower()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        try:
            value = value.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("通知服务器地址无效") from None
        if "." not in value or value.endswith((".localhost", ".local", ".internal", ".home.arpa")):
            raise ValueError(TARGET_ERROR)
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in value.split(".")):
            raise ValueError("通知服务器地址无效")
        return value
    return public_ip(value)


def webhook_target(url):
    if not isinstance(url, str) or len(url) > 4096 or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url:
        raise ValueError("推送地址无效")
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError()
        # Notification webhooks use HTTPS; arbitrary service ports are disallowed.
        if parts.port not in (None, 443):
            raise ValueError()
    except ValueError:
        raise ValueError("推送地址必须使用 HTTPS 标准端口且不包含用户名、密码或片段") from None
    return hostname(parts.hostname)


def smtp_target(config):
    host = hostname(config.get("host"))
    try:
        port = int(config.get("port", 465))
        if isinstance(config.get("port"), bool) or str(config.get("port", 465)) != str(port) or port not in (465, 587):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("邮件服务器仅支持加密端口 465 或 587") from None
    return host, port


def validate_config(channel, config):
    if "enabled" in config and type(config["enabled"]) is not bool:
        raise ValueError("通知启用选项必须为布尔值")
    for field in ("url", "host", "user", "to", "password"):
        if field in config and (not isinstance(config[field], str) or len(config[field]) > 4096):
            raise ValueError("通知配置格式无效或内容过长")
    if channel == "email":
        if config.get("host") or config.get("enabled"):
            smtp_target(config)
    elif config.get("url") or config.get("enabled"):
        webhook_target(config.get("url", ""))


async def resolve_public(host, port):
    host = hostname(host)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            records = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5
            )
        except (OSError, TimeoutError):
            raise ValueError("通知服务器域名解析失败，请检查地址") from None
        if not records:
            raise ValueError("通知服务器域名解析失败，请检查地址")
        # Reject mixed public/private answers as well as wholly private DNS.
        addresses = [public_ip(record[4][0]) for record in records]
        return addresses[0]
    return public_ip(host)


class PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, host, address):
        super().__init__(trust_env=False, retries=0)
        self.host, self.address = host, public_ip(address)

    async def handle_async_request(self, request):
        if webhook_target(str(request.url)) != self.host:
            raise ValueError("通知目标发生变化")
        # Connect only to the checked numeric IP. TLS verifies the original host.
        request.headers["Host"] = request.url.netloc.decode("ascii")
        request.extensions["sni_hostname"] = self.host
        request.url = request.url.copy_with(host=self.address)
        return await super().handle_async_request(request)


async def post_json(url, **payload):
    try:
        async with asyncio.timeout(20):
            return await _post_json(url, **payload)
    except TimeoutError:
        raise ValueError("通知发送超时，请稍后重试") from None


async def _post_json(url, **payload):
    host = webhook_target(url)
    address = await resolve_public(host, 443)
    transport = PinnedTransport(host, address)
    async with httpx.AsyncClient(transport=transport, timeout=15, follow_redirects=False, trust_env=False) as client:
        async with client.stream("POST", url, headers={"Accept-Encoding": "identity"}, **payload) as response:
            response.raise_for_status()  # Redirects are errors, never followed.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("通知服务返回了不支持的压缩响应")
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_RESPONSE:
                    raise ValueError("通知服务响应过大")
                body.extend(chunk)
    try:
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("通知服务返回了无效响应") from None


class PinnedSMTP(smtplib.SMTP):
    def __init__(self, host, port, address):
        self.address = public_ip(address)
        super().__init__(host, port, timeout=15, local_hostname="localhost")

    def _get_socket(self, host, port, timeout):
        return socket.create_connection((self.address, port), timeout, self.source_address)


class PinnedSMTPSSL(PinnedSMTP):
    def _get_socket(self, host, port, timeout):
        sock = super()._get_socket(host, port, timeout)
        try:
            return ssl.create_default_context().wrap_socket(sock, server_hostname=self._host)
        except BaseException:
            sock.close()
            raise


async def send_email(config, message):
    host, port = smtp_target(config)
    address = await resolve_public(host, port)

    def send():
        client = None
        try:
            client = (PinnedSMTPSSL if port == 465 else PinnedSMTP)(host, port, address)
            if port == 587:
                client.starttls(context=ssl.create_default_context())
            client.login(config["user"], config["password"])
            client.send_message(message)
        finally:
            if client:
                client.close()

    await asyncio.to_thread(send)
