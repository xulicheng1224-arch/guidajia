"""Present operational evidence without exposing the service host or filesystem.

Stored records and private operator logs remain unchanged. Apply this only to
generated technical data, never to user names, notes or configured endpoints.
"""

import ipaddress
import json
import re

PRIVATE_KEYS = {
    "browser", "browser_headed", "headless", "framework", "server_os", "os_version", "os", "system",
    "operating_system", "system_info", "system_version", "hostname", "host_name",
    "server_name", "server_address", "server_ip", "local_ip", "architecture", "machine",
    "python_version", "node_version", "kernel_version", "system_release", "platform_version",
    "server_version", "pid", "process_id", "cwd", "root", "data_dir",
    "path", "profile_path", "storage_path", "storage_dir", "executable", "executable_path",
    "traceback", "stacktrace", "stack", "environment",
}
USER_FIELDS = {"name", "product_name", "product_context", "selected_options", "expected_options", "reason"}
_PATH = re.compile(r'''(?<![\w/])(?:/(?:Users|home|private|opt|Applications|usr|var|tmp|etc)/|[A-Za-z]:[\\/]|\\\\)[^\s"'<>，；,;\]\)\}]+''')
_QUOTED_PATH = re.compile(r'''(["'])(?:/(?:Users|home|private|opt|Applications|usr|var|tmp|etc)/|[A-Za-z]:[\\/]|\\\\)[^"'\r\n]+\1''')
_IP = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_HOST = re.compile(r"\b[\w.-]+\.local\b", re.I)


def text(value):
    if not isinstance(value, str):
        return value
    for old, new in (
        ("运行采集的 Mac 浏览器", "采集浏览器"),
        ("运行服务的 Mac", "采集端"), ("服务器 Mac", "采集端"),
        ("管理员的 Mac 会话", "管理员会话"),
        ("在 Mac 浏览器", "在采集浏览器"), ("在 Mac 上", "在采集端"),
        ("在 Mac ", "在浏览器 "), ("本机浏览器", "采集浏览器"),
        ("服务器训练名额", "训练名额"),
    ):
        value = value.replace(old, new)
    value = _QUOTED_PATH.sub(lambda m: m[1] + "[内部路径]" + m[1], value)
    value = _PATH.sub("[内部路径]", value)
    value = _HOST.sub("[设备]", value)
    def address(match):
        try:
            return "[内部地址]" if not ipaddress.ip_address(match[0]).is_global else match[0]
        except ValueError:
            return match[0]
    return _IP.sub(address, value)


def technical(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in PRIVATE_KEYS:
                continue
            if key in USER_FIELDS:
                result[key] = business(item)
                continue
            if key == "platform" and isinstance(item, str) and item.lower() in ("darwin", "linux", "win32", "windows", "macos"):
                continue
            # Job results and contexts are stored as JSON strings. Sanitizing
            # only their surrounding object would leave old metadata exposed.
            if key in ("result", "context") and isinstance(item, str):
                try:
                    result[key] = json.dumps(technical(json.loads(item)), ensure_ascii=False)
                    continue
                except (ValueError, TypeError):
                    pass
            result[key] = technical(item)
        return result
    if isinstance(value, (list, tuple)):
        return [technical(item) for item in value]
    return text(value)


def business(value):
    """Keep user-authored values intact, sanitizing embedded diagnostics only."""
    if isinstance(value, dict):
        return {key: technical(item) if key in ("diagnostics", "last_diagnostics")
                else business(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [business(item) for item in value]
    return value


def scrub_database(db, include_runtime=False):
    """Sanitize a download copy, including backups made before this policy."""
    db.execute("PRAGMA secure_delete=ON")
    for table, columns in (("logs", ("message", "details")),
                           ("observations", ("data",)), ("jobs", ("result", "context"))):
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in present:
                continue
            cursor = db.execute(f"SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL")
            while rows := cursor.fetchmany(250):
                updates = []
                for row_id, raw in rows:
                    if column == "message":
                        cleaned = text(raw)
                    else:
                        try:
                            cleaned = json.dumps(technical(json.loads(raw)), ensure_ascii=False)
                        except (ValueError, TypeError):
                            cleaned = text(raw)
                    if cleaned != raw:
                        updates.append((cleaned, row_id))
                db.executemany(f"UPDATE {table} SET {column}=? WHERE id=?", updates)
    # Plain account backups exclude credentials. The administrator's encrypted
    # full-system backup remains a complete, private recovery artifact.
    db.execute("UPDATE settings SET value='{}' WHERE key IN ('channels','price_apis')")
    db.execute("UPDATE settings SET value='\"crawler\"' WHERE key='price_source'")
    if not include_runtime:
        db.execute("DELETE FROM settings WHERE key IN ('browser','crawler_headed')")
    db.commit()
    db.execute("VACUUM")
