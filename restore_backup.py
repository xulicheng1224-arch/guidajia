"""Safely restore a database backup while the service is stopped."""

import argparse
import fcntl
import os
import shutil
import sqlite3
import tempfile
import getpass
from datetime import datetime
from pathlib import Path


def restore(source, folder):
    source = Path(source).resolve()
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("监测服务正在运行，请先双击「停止监测.command」再恢复")
        if not source.is_file():
            raise ValueError("备份文件不存在")
        check = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("数据库完整性检查失败")
            tables = {
                r[0]
                for r in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"entities", "observations", "settings", "audit", "jobs"}.issubset(
                tables
            ):
                raise ValueError("不是有效的鬼打架数据库备份")
        finally:
            check.close()
        fd, temporary = tempfile.mkstemp(dir=folder, prefix=".restore-")
        os.close(fd)
        target = folder / "monitor.sqlite3"
        try:
            shutil.copy2(source, temporary)
            os.chmod(temporary, 0o600)
            if target.exists():
                # Keep one pre-restore recovery snapshot.
                old = sqlite3.connect(target)
                saved = sqlite3.connect(
                    folder / f"before-restore-{datetime.now():%Y%m%d-%H%M%S}.sqlite3"
                )
                try:
                    old.backup(saved)
                finally:
                    saved.close()
                    old.close()
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--full", action="store_true", help="恢复密码加密的完整系统备份")
    parser.add_argument("--check-only", action="store_true", help="仅校验完整备份，不更改数据")
    parser.add_argument(
        "--data-dir", type=Path, default=Path(__file__).resolve().parent / "data"
    )
    args = parser.parse_args()
    if args.check_only and not (args.full or args.backup.suffix == ".gdbak"):
        parser.error("--check-only 仅用于 .gdbak 完整备份，不会恢复普通数据库")
    try:
        if args.full or args.backup.suffix == ".gdbak":
            from backend import full_backup
            password = getpass.getpass("完整备份密码：")
            if args.check_only:
                result = full_backup.check(args.backup, password)
                print(f"备份校验通过：包含 {result['user_count']} 个账号的数据。")
            else:
                result = full_backup.restore(args.backup, args.data_dir, password)
                print(f"完整恢复完成。恢复前副本：{result['rollback_directory']}。可重新启动服务，各账号需手动恢复采集。")
        else:
            restore(args.backup, args.data_dir)
            print("恢复完成，可以重新启动监测服务。")
    except (ValueError, sqlite3.DatabaseError) as exc:
        parser.exit(1, f"恢复失败：{exc}\n")
