"""Password-encrypted system snapshots and validated, rollback-safe offline restore."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import accounts, session_state, store as s, training_queue

MAGIC = b"GDJBAK1\x00"
CHUNK = 1024 * 1024
MAX_BYTES = 10 * 1024 ** 3
_USER = r"[a-f0-9]{32}"
_MODEL = r"[a-f0-9]{32}/(?:classifier\.txt|q10\.txt|q50\.txt|q90\.txt|metadata\.json)"


def password_value(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("备份密码须为 12–256 位，请妥善保存，丢失后无法解密")
    return password.encode("utf-8")


def _key(password, salt):
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password_value(password))


def encrypt(source, target, password):
    salt, prefix = os.urandom(16), os.urandom(8)
    header = MAGIC + salt + prefix
    cipher = AESGCM(_key(password, salt))
    with open(source, "rb") as incoming, open(target, "wb") as outgoing:
        os.chmod(target, 0o600)
        outgoing.write(header)
        index = 0
        while True:
            chunk = incoming.read(CHUNK)
            counter = struct.pack(">I", index)
            encrypted = cipher.encrypt(prefix + counter, chunk, header + counter)
            outgoing.write(struct.pack(">I", len(encrypted)))
            outgoing.write(encrypted)
            index += 1
            if not chunk:
                break


def decrypt(source, target, password):
    with open(source, "rb") as incoming, open(target, "wb") as outgoing:
        os.chmod(target, 0o600)
        header = incoming.read(len(MAGIC) + 24)
        if len(header) != len(MAGIC) + 24 or not header.startswith(MAGIC):
            raise ValueError("不是有效的鬼打架完整备份")
        cipher = AESGCM(_key(password, header[len(MAGIC):len(MAGIC) + 16]))
        prefix = header[-8:]
        total = 0
        index = 0
        while True:
            size = incoming.read(4)
            if len(size) != 4:
                raise ValueError("备份文件不完整")
            length = struct.unpack(">I", size)[0]
            if not 16 <= length <= CHUNK + 16:
                raise ValueError("备份分段格式无效")
            encrypted = incoming.read(length)
            if len(encrypted) != length:
                raise ValueError("备份文件不完整")
            counter = struct.pack(">I", index)
            try:
                chunk = cipher.decrypt(prefix + counter, encrypted, header + counter)
            except InvalidTag:
                raise ValueError("备份密码错误或文件已被修改") from None
            index += 1
            if not chunk:
                if incoming.read(1):
                    raise ValueError("备份文件包含无效尾部数据")
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("备份超过允许的解密大小")
            outgoing.write(chunk)


def allowed(name, cookies=True):
    if name in ("accounts.sqlite3", "monitor.sqlite3", "public-access.json"):
        return True
    prefix = rf"(?:users/{_USER}/)?"
    return bool(re.fullmatch(rf"users/{_USER}/monitor\.sqlite3", name)
        or re.fullmatch(prefix + rf"forecast-models/(?:active\.json|{_MODEL})", name)
        or (cookies and re.fullmatch(prefix + r"auth/taobao\.json", name)))


def _snapshot_database(source, target, account=False):
    if source.is_symlink() or not source.resolve().is_relative_to(s.DATA.resolve()):
        raise ValueError("备份数据库不能指向用户数据目录之外")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    saved = sqlite3.connect(target)
    try:
        original.backup(saved)
        if account:
            # Restoring a snapshot must never resurrect a prior web login.
            saved.execute("DELETE FROM sessions")
        saved.commit()
        saved.execute("PRAGMA journal_mode=DELETE")
    finally:
        saved.close()
        original.close()
    target.chmod(0o600)


def _copy(source, target):
    if source.is_symlink() or not source.resolve().is_relative_to(s.DATA.resolve()):
        raise ValueError("备份来源不能是符号链接")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o600)


def _snapshot(folder, include_cookies):
    # Lock order matches all application writes. Compression/encryption happen
    # after the short snapshot, so normal DB writes can then resume.
    with training_queue.EXECUTION_LOCK, accounts.LOCK, s.LOCK, session_state._LOCK:
        people = accounts.users()
        _snapshot_database(s.DATA / "accounts.sqlite3", folder / "accounts.sqlite3", True)
        for user in people:
            owner = user["id"]
            prefix = Path() if owner == "admin" else Path("users") / owner
            with s.tenant_scope(owner):
                _snapshot_database(s.db_path(), folder / prefix / "monitor.sqlite3")
                models = s.data_dir() / "forecast-models"
                if models.is_dir():
                    for path in models.rglob("*"):
                        relative = (prefix / "forecast-models" / path.relative_to(models)).as_posix()
                        if path.is_file() and allowed(relative, False):
                            _copy(path, folder / relative)
                if include_cookies:
                    path = s.data_dir() / "auth" / "taobao.json"
                    if path.is_file():
                        _copy(path, folder / prefix / "auth" / "taobao.json")
        config = s.DATA / "public-access.json"
        if config.is_file():
            _copy(config, folder / "public-access.json")
    return len(people)


def create(password, include_cookies=False):
    password_value(password)
    directory = s.DATA / "system-backups"
    directory.mkdir(mode=0o700, exist_ok=True)
    name = "guidajia-full-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + s.uid()[:8] + ".gdbak"
    target = directory / name
    temporary = directory / ("." + name + ".partial")
    try:
        with tempfile.TemporaryDirectory(prefix=".system-snapshot-", dir=s.DATA) as work:
            folder = Path(work) / "files"
            folder.mkdir(mode=0o700)
            user_count = _snapshot(folder, include_cookies)
            files = {}
            for path in folder.rglob("*"):
                if path.is_file():
                    files[path.relative_to(folder).as_posix()] = {"size": path.stat().st_size, "sha256": digest(path)}
            manifest = {"format": "guidajia-system-backup", "version": 1, "created_at": s.now(),
                        "include_cookies": bool(include_cookies), "user_count": user_count, "files": files}
            archive = Path(work) / "snapshot.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("manifest.json", json.dumps(manifest))
                for name_in_zip in files:
                    bundle.write(folder / name_in_zip, name_in_zip)
            encrypt(archive, temporary, password)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"name": name, "size": target.stat().st_size, "created_at": manifest["created_at"], "include_cookies": bool(include_cookies), "user_count": user_count}


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as incoming:
        for chunk in iter(lambda: incoming.read(CHUNK), b""):
            value.update(chunk)
    return value.hexdigest()


def _check_database(path, required):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性检查失败")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(tables):
            raise ValueError("备份缺少必要数据库表")
    finally:
        db.close()


def _unpack(source, password, folder):
    archive = folder / "decrypted.zip"
    decrypt(source, archive, password)
    result = folder / "data"
    result.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or len(names) > 100000 or "manifest.json" not in names:
            raise ValueError("备份目录包含重复文件或无效清单")
        if bundle.getinfo("manifest.json").file_size > 20 * CHUNK or sum(info.file_size for info in infos) > MAX_BYTES:
            raise ValueError("备份展开大小超过限制")
        manifest = json.loads(bundle.read("manifest.json"))
        if manifest.get("format") != "guidajia-system-backup" or manifest.get("version") != 1:
            raise ValueError("不支持的完整备份版本")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(names) != {"manifest.json", *files} or not {"accounts.sqlite3", "monitor.sqlite3"}.issubset(files):
            raise ValueError("备份文件清单不完整")
        for info in infos:
            name = info.filename
            if name == "manifest.json":
                continue
            if not allowed(name, manifest.get("include_cookies") is True) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("备份包含不允许的文件路径")
            target = result / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with bundle.open(info) as incoming, target.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, CHUNK)
            target.chmod(0o600)
            if target.stat().st_size != files[name].get("size") or digest(target) != files[name].get("sha256"):
                raise ValueError("备份文件校验失败")
            if name.endswith(".sqlite3"):
                _check_database(target, {"users", "sessions"} if name == "accounts.sqlite3" else {"entities", "observations", "settings", "jobs", "audit"})
    db = sqlite3.connect(result / "accounts.sqlite3")
    try:
        users = db.execute("SELECT id,email,role FROM users").fetchall()
        admins = [row for row in users if row[2] == "admin"]
        if len(admins) != 1 or admins[0][0] != "admin" or admins[0][1].lower() != accounts.ADMIN_EMAIL.lower():
            raise ValueError("备份的唯一管理员信息无效")
        ids = {row[0] for row in users}
        if any(owner != "admin" and not re.fullmatch(_USER, owner) for owner in ids):
            raise ValueError("备份的用户标识无效")
        if any(not (result / "users" / owner / "monitor.sqlite3").is_file() for owner in ids if owner != "admin"):
            raise ValueError("备份缺少用户工作空间")
        if any(name.startswith("users/") and name.split("/")[1] not in ids for name in files):
            raise ValueError("备份的用户与目录映射不一致")
    finally:
        db.close()
    return result, manifest


def unpack(source, password, folder):
    try:
        return _unpack(source, password, folder)
    except (KeyError, TypeError, AttributeError, sqlite3.DatabaseError, zipfile.BadZipFile):
        raise ValueError("备份内容结构无效，校验未通过") from None


def check(source, password):
    with tempfile.TemporaryDirectory(prefix="guidajia-check-") as folder:
        _, manifest = unpack(source, password, Path(folder))
        return {key: manifest[key] for key in ("created_at", "include_cookies", "user_count", "version")}


def restore(source, folder, password):
    source, folder = Path(source).resolve(), Path(folder).resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (folder / "service.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("监测服务正在运行，请先停止服务再恢复完整备份") from None
        with tempfile.TemporaryDirectory(prefix=".full-restore-", dir=folder.parent) as work:
            staged, manifest = unpack(source, password, Path(work))
            # Preserve queued work, but require users to explicitly resume it.
            for database in staged.rglob("*.sqlite3"):
                with sqlite3.connect(database) as db:
                    if database.name == "accounts.sqlite3":
                        db.execute("DELETE FROM sessions")
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='training_queue'").fetchone():
                            db.execute("UPDATE training_queue SET state='queued',started_at=NULL WHERE state='running'")
                    else:
                        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('global_paused','true')")
                        db.execute("UPDATE jobs SET status='queued',started_at=NULL WHERE status='running'")
                        db.execute("UPDATE model_runs SET status='interrupted' WHERE status='running'")
            (staged / "restored-state.json").write_text(json.dumps({"restored_at": datetime.now(timezone.utc).isoformat(), "include_cookies": manifest["include_cookies"]}))
            (staged / "restored-state.json").chmod(0o600)
            rollback = folder.parent / (folder.name + "-before-full-restore-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(4).hex())
            rollback.mkdir(mode=0o700)
            moved_old, moved_new = [], []
            try:
                for path in list(folder.iterdir()):
                    if path.name != "service.lock":
                        path.rename(rollback / path.name)
                        moved_old.append(path.name)
                for path in staged.iterdir():
                    path.rename(folder / path.name)
                    moved_new.append(path.name)
            except BaseException:
                for name in reversed(moved_new):
                    target = folder / name
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                for name in reversed(moved_old):
                    (rollback / name).rename(folder / name)
                raise
            return {"rollback_directory": str(rollback), "user_count": manifest["user_count"], "paused": True}
