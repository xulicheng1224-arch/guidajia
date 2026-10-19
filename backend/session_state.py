"""Short, atomic session updates; browser tasks use independent cookie copies."""

import json
import math
import re
import threading
from collections import defaultdict
from urllib.parse import urlsplit

from crawler import save_json

_LOCK = threading.RLock()
_GENERATION = defaultdict(int)
_REFRESH_ORDER = defaultdict(int)
_UPDATED = defaultdict(dict)
ALLOWED_COOKIE_DOMAINS = ("taobao.com", "tmall.com")


def _allowed_domain(value):
    original = str(value or "").strip().lower()
    domain = original.lstrip(".")
    if not domain or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for ch in domain):
        return None
    if not any(domain == root or domain.endswith("." + root) for root in ALLOWED_COOKIE_DOMAINS):
        return None
    return ("." if original.startswith(".") else "") + domain


def normalize_uploaded(value):
    """Normalize browser-exported cookies without retaining unrelated domains."""
    if not isinstance(value, str):
        raise ValueError("Cookie 文件内容必须是文本")
    if len(value.encode("utf-8")) > 5 * 1024 * 1024:
        raise ValueError("Cookie 文件不能超过 5 MB")
    text = value.strip()
    parsed = None
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Cookie JSON 格式无效") from exc
    if isinstance(parsed, dict):
        raw_cookies = parsed.get("cookies")
        if raw_cookies is None:
            raw_cookies = parsed.get("cookie")
        origins = parsed.get("origins", [])
    elif isinstance(parsed, list):
        raw_cookies, origins = parsed, []
    else:
        # Also accept a copied Cookie request header: name=value; name2=value2.
        raw_cookies, origins = [], []
        for part in re.split(r"[;\n]+", text):
            if "=" not in part:
                continue
            name, cookie_value = part.split("=", 1)
            name, cookie_value = name.strip(), cookie_value.strip()
            if name:
                raw_cookies.append({"name": name, "value": cookie_value, "domain": ".taobao.com", "path": "/"})
    if not isinstance(raw_cookies, list) or not raw_cookies:
        raise ValueError("未找到 Cookie 数组；请上传浏览器导出的 JSON 或 Cookie 文本")
    cookies = {}
    rejected = 0
    for item in raw_cookies:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            rejected += 1
            continue
        name = str(item["name"]).strip()
        cookie_value = item.get("value", "")
        if not isinstance(cookie_value, str):
            cookie_value = str(cookie_value)
        domain = _allowed_domain(item.get("domain"))
        if not domain:
            # Chrome exports may omit domain only for a copied Cookie header.
            domain = ".taobao.com" if not item.get("domain") else None
        if not domain:
            rejected += 1
            continue
        cookie = {
            "name": name,
            "value": cookie_value,
            "domain": domain,
            "path": str(item.get("path") or "/"),
            "secure": bool(item.get("secure", True)),
            "httpOnly": bool(item.get("httpOnly", False)),
        }
        same_site = item.get("sameSite")
        if same_site in ("Strict", "Lax", "None"):
            cookie["sameSite"] = same_site
        partition = item.get("partitionKey")
        if partition is not None:
            site = partition.get("topLevelSite") if isinstance(partition, dict) else partition
            try:
                parsed = urlsplit(site) if isinstance(site, str) else None
                if (not parsed or parsed.scheme != "https" or not _allowed_domain(parsed.hostname)
                        or parsed.username or parsed.password or parsed.port not in (None, 443)
                        or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                    rejected += 1
                    continue
            except ValueError:
                rejected += 1
                continue
            cookie["partitionKey"] = f"https://{parsed.hostname}"
            ancestor = partition.get("hasCrossSiteAncestor") if isinstance(partition, dict) else item.get("_crHasCrossSiteAncestor")
            if isinstance(ancestor, bool):
                cookie["_crHasCrossSiteAncestor"] = ancestor
        expires = item.get("expires", item.get("expirationDate"))
        if expires not in (None, "", 0, -1):
            try:
                expiration = float(expires)
                if not math.isfinite(expiration):
                    rejected += 1
                    continue
                cookie["expires"] = expiration
            except (TypeError, ValueError):
                rejected += 1
                continue
        if expires == -1:
            cookie["expires"] = -1
        key = (name, domain, cookie["path"], cookie.get("partitionKey"), cookie.get("_crHasCrossSiteAncestor"))
        cookies[key] = cookie
    if not cookies:
        raise ValueError("没有找到淘宝或天猫域名的有效 Cookie")
    # Storage state is optional; only retain localStorage for Taobao/Tmall
    # origins so an uploaded file cannot seed unrelated browser data.
    safe_origins = []
    if isinstance(origins, list):
        for origin in origins:
            if not isinstance(origin, dict):
                continue
            url = str(origin.get("origin", ""))
            host = (urlsplit(url).hostname or "").lower()
            if not any(host == root or host.endswith("." + root) for root in ALLOWED_COOKIE_DOMAINS):
                continue
            items = origin.get("localStorage", [])
            if isinstance(items, list):
                safe_items = [
                    {"name": str(x["name"]), "value": str(x.get("value", ""))}
                    for x in items
                    if isinstance(x, dict) and str(x.get("name", "")).strip()
                ]
                safe_origins.append({"origin": url, "localStorage": safe_items})
    return {"cookies": list(cookies.values()), "origins": safe_origins}, rejected


def import_diagnostics(content, state):
    """Log allowlisted coverage metadata, never arbitrary upload fields/values."""
    try:
        raw = json.loads(content)
    except (ValueError, TypeError):
        raw = None
    info = raw.get("export_info") if isinstance(raw, dict) else None
    info = info if isinstance(info, dict) else {}
    version = info.get("version")
    extension = info.get("tool") == "guidajia-cookie-extension"
    version = version if extension and isinstance(version, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version) else None

    def reported_count(key):
        count = info.get(key)
        return count if extension and type(count) is int and 0 <= count <= 100000 else None

    cookies = state["cookies"]
    origins = {origin["origin"] for origin in state.get("origins", [])}
    count = reported_count("cookie_count")
    return {
        "source": "guidajia-cookie-extension" if extension else "external",
        "source_version": version,
        "cookie_count": len(cookies),
        "partitioned_cookie_count": sum(bool(c.get("partitionKey")) for c in cookies),
        "storage_origin_count": len(origins),
        "reported_storage_read_failures": reported_count("storage_read_failures"),
        "reported_storage_frames_read": reported_count("storage_frames_read"),
        "cookie_count_mismatch": count is not None and count != len(cookies),
        "auth_markers": {root: [name for name in ("unb", "_nk_", "lgc") if any(
            c["name"] == name and c["value"] and c["domain"] in (root, "." + root)
            for c in cookies)] for root in ALLOWED_COOKIE_DOMAINS},
        # Missing origins describe coverage, not login validity.
        "missing_auth_storage_origins": [origin for origin in (
            "https://login.taobao.com", "https://passport.taobao.com") if origin not in origins],
    }


def snapshot(path):
    with _LOCK:
        state = json.loads(path.read_text()) if path.is_file() else None
        return _GENERATION[str(path)], state


def generation(path):
    with _LOCK:
        return _GENERATION[str(path)]


def refresh_snapshot(path):
    """Give each browser copy an order so slow older copies cannot win races."""
    with _LOCK:
        current, state = snapshot(path)
        _REFRESH_ORDER[str(path)] += 1
        return current, state, _REFRESH_ORDER[str(path)]


def save(path, state, *, expected_generation=None):
    """Every accepted source replaces the session; stale login windows cannot."""
    with _LOCK:
        if expected_generation is not None and _GENERATION[str(path)] != expected_generation:
            return False
        save_json(path, state)
        path.chmod(0o600)
        _GENERATION[str(path)] += 1
        _UPDATED.pop(str(path), None)
        path.with_suffix(".cleared").unlink(missing_ok=True)
        return True


def can_migrate(path):
    """An explicit clear must not resurrect a legacy session after restart."""
    with _LOCK:
        return not path.exists() and not path.with_suffix(".cleared").exists()


def clear(path):
    with _LOCK:
        save_json(path.with_suffix(".cleared"), {"cleared": True})
        path.with_suffix(".cleared").chmod(0o600)
        _GENERATION[str(path)] += 1
        _UPDATED.pop(str(path), None)
        path.unlink(missing_ok=True)


def _merge_items(before, current, incoming, key, updated, order):
    base = {key(item): item for item in before}
    live = {key(item): item for item in current}
    after = {key(item): item for item in incoming}
    # A browser may omit expired/domain-scoped records from its partial state.
    # Absence is not a new Cookie: only an explicit upload/login replaces the
    # full state. Keep other saved records until that happens or a user clears.
    for name in after:
        # Only a changed value is a browser refresh. A slower, older browser
        # may add unrelated values, but cannot overwrite a newer copy's update.
        if after.get(name) != base.get(name) and order >= updated.get(name, -1):
            live[name] = after[name]
            updated[name] = order
    return list(live.values())


def merge(path, generation, baseline, incoming, *, order=None):
    with _LOCK:
        if _GENERATION[str(path)] != generation:
            return False
        if order is None:
            _REFRESH_ORDER[str(path)] += 1
            order = _REFRESH_ORDER[str(path)]
        updated = _UPDATED[str(path)]
        current = json.loads(path.read_text()) if path.is_file() else {}
        base = baseline or {}
        cookie_key = lambda c: (
            c["name"],
            c["domain"],
            c.get("path", "/"),
            json.dumps(c.get("partitionKey"), sort_keys=True),
            c.get("_crHasCrossSiteAncestor"),
        )
        merged = {
            **current,
            "cookies": _merge_items(
                base.get("cookies", []),
                current.get("cookies", []),
                incoming.get("cookies", []),
                cookie_key,
                updated.setdefault("cookies", {}),
                order,
            ),
            "origins": _merge_items(
                base.get("origins", []),
                current.get("origins", []),
                incoming.get("origins", []),
                lambda o: o["origin"],
                updated.setdefault("origins", {}),
                order,
            ),
        }
        if merged != current:
            save_json(path, merged)
            path.chmod(0o600)
        return True
