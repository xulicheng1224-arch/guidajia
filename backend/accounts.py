"""Account identity is global; application data belongs to one tenant only."""

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from fastapi import HTTPException

from . import store as s

COOKIE = "monitor_account_session"
# Public builds use a neutral address. Set MONITOR_ADMIN_EMAIL before creating
# the first administrator if you want a different sign-in identity.
ADMIN_EMAIL = os.environ.get("MONITOR_ADMIN_EMAIL", "admin@example.com").strip().lower()
SESSION_SECONDS = 12 * 3600
LOCK = threading.RLock()


@contextmanager
def connection():
    with LOCK:
        path = s.DATA / "accounts.sqlite3"
        db = sqlite3.connect(path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            path.chmod(0o600)


def init():
    with connection() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(
          id TEXT PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
          password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')),
          email_verified INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, last_login TEXT, auth_version INTEGER NOT NULL DEFAULT 1,
          CHECK((role='admin' AND id='admin') OR (role='user' AND id<>'admin')));
        CREATE UNIQUE INDEX IF NOT EXISTS sole_admin ON users(role) WHERE role='admin';
        CREATE TABLE IF NOT EXISTS sessions(
          digest TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL,
          expires_at REAL NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE INDEX IF NOT EXISTS session_user ON sessions(user_id);
        CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS training_queue(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,state TEXT NOT NULL,requested_at TEXT NOT NULL,
          started_at TEXT,finished_at TEXT,configuration TEXT NOT NULL,result TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_training_request_per_user ON training_queue(user_id)
          WHERE state IN ('queued','running');
        """)



def email_address(value):
    if not isinstance(value, str):
        raise HTTPException(422, "请输入有效邮箱地址")
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise HTTPException(422, "请输入有效邮箱地址")
    local, domain = value.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in domain.split(".")
    ):
        raise HTTPException(422, "请输入有效邮箱地址")
    return value.lower()


def password_hash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise HTTPException(422, "密码须为 8–128 位，并包含字母和数字")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def check_password(password, encoded):
    if not isinstance(password, str) or len(password) > 128:
        return False
    try:
        scheme, rounds, salt, digest = encoded.split("$")
        if scheme != "pbkdf2_sha256" or int(rounds) != 600000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return secrets.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


def provision_admin(password):
    """Explicit one-time setup. Existing credentials are never reset at startup."""
    init()
    encoded = password_hash(password)
    with connection() as db:
        if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            raise ValueError("管理员已存在")
        db.execute("INSERT INTO users(id,email,password_hash,role,email_verified,created_at) VALUES('admin',?,?,'admin',1,?)", (ADMIN_EMAIL, encoded, s.now()))


def public_user(row):
    return {key: row[key] for key in ("id", "email", "role", "email_verified", "disabled", "created_at", "last_login")}


def users():
    with connection() as db:
        return [public_user(row) for row in db.execute("SELECT * FROM users ORDER BY created_at")]


def active_tenants():
    with connection() as db:
        return [row[0] for row in db.execute("SELECT id FROM users WHERE disabled=0 ORDER BY created_at")]


def limit(bucket, maximum, seconds):
    stamp = time.time()
    key = hashlib.sha256(bucket.encode()).hexdigest()
    with connection() as db:
        db.execute("DELETE FROM limits WHERE expires<?", (stamp,))
        row = db.execute("SELECT count FROM limits WHERE key=?", (key,)).fetchone()
        if row and row[0] >= maximum:
            raise HTTPException(429, "尝试过多，请稍后再试", headers={"Retry-After": str(seconds)})
        db.execute("INSERT INTO limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1", (key, stamp + seconds))


def authenticate(email, password):
    email = email_address(email)
    with connection() as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    # Equal work for unknown users prevents the cheap timing distinction.
    dummy = "pbkdf2_sha256$600000$" + "0" * 32 + "$" + "0" * 64
    valid = check_password(password, user["password_hash"] if user else dummy)
    if not user or not valid or user["disabled"]:
        raise HTTPException(401, "邮箱或密码错误，或账号已停用")
    return public_user(user), user["auth_version"]


def clear_login_failures(email):
    with connection() as db:
        db.execute("DELETE FROM limits WHERE key=?", (hashlib.sha256(("login-email:" + email).encode()).hexdigest(),))


def create_session(user_id, remember=False, version=None):
    token = secrets.token_urlsafe(32)
    duration = 30 * 86400 if remember else SESSION_SECONDS
    stamp = time.time()
    with connection() as db:
        row = db.execute("SELECT auth_version FROM users WHERE id=? AND disabled=0", (user_id,)).fetchone()
        if not row or (version is not None and row[0] != version):
            raise HTTPException(401, "账号凭据已变化，请重新登录")
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (stamp,))
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, stamp, stamp + duration))
        db.execute("UPDATE users SET last_login=? WHERE id=?", (s.now(), user_id))
    return token, duration


def session_user(token):
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        return None
    with connection() as db:
        row = db.execute("SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE digest=? AND expires_at>? AND disabled=0", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    return public_user(row) if row else None


def logout(token):
    with connection() as db:
        db.execute("DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))


def register(email, password):
    email = email_address(email)
    if email == ADMIN_EMAIL.lower():
        raise HTTPException(409, "该邮箱为管理员账号，请直接登录")
    encoded = password_hash(password)
    with connection() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "该邮箱已注册，请直接登录")
    user_id = s.uid()
    with s.tenant_scope(user_id):
        s.init()
    try:
        with connection() as db:
            db.execute("INSERT INTO users(id,email,password_hash,role,email_verified,created_at) VALUES(?,?,?,'user',?,?)", (user_id, email, encoded, 0, s.now()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该邮箱已注册，请直接登录") from None
    return user_id


def change_password(user_id, old_password, new_password):
    with connection() as db:
        user = db.execute("SELECT password_hash FROM users WHERE id=? AND disabled=0", (user_id,)).fetchone()
    if not user or not check_password(old_password, user[0]):
        raise HTTPException(422, "当前密码不正确")
    encoded = password_hash(new_password)
    with connection() as db:
        updated = db.execute("UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE id=? AND password_hash=?", (encoded, user_id, user[0]))
        if not updated.rowcount:
            raise HTTPException(409, "账号密码已变化，请重新登录")
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def admin_reset_password(user_id, password):
    if user_id == "admin":
        raise HTTPException(403, "管理员请使用当前密码修改自己的密码")
    encoded = password_hash(password)
    with connection() as db:
        if not db.execute("SELECT 1 FROM users WHERE id=? AND role='user'", (user_id,)).fetchone():
            raise HTTPException(404, "账号不存在")
        db.execute("UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE id=?", (encoded, user_id))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def set_disabled(user_id, disabled):
    if user_id == "admin":
        raise HTTPException(403, "唯一管理员账号不能停用")
    if type(disabled) is not bool:
        raise HTTPException(422, "账号状态无效")
    with connection() as db:
        if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "账号不存在")
        db.execute("UPDATE users SET disabled=?,auth_version=auth_version+1 WHERE id=?", (int(disabled), user_id))
        if disabled:
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
