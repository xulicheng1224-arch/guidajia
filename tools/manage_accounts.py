"""Local account recovery; credentials are read privately from the terminal."""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import accounts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create-admin", "reset-admin"))
    args = parser.parse_args()
    password = getpass.getpass("管理员新密码：")
    if password != getpass.getpass("再次输入："):
        raise SystemExit("两次输入不一致")
    if args.action == "create-admin":
        accounts.provision_admin(password)
    else:
        accounts.init()
        encoded = accounts.password_hash(password)
        with accounts.connection() as db:
            if not db.execute("SELECT 1 FROM users WHERE id='admin'").fetchone():
                raise SystemExit("管理员尚未创建，请先执行 create-admin")
            db.execute("UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE id='admin'", (encoded,))
            db.execute("DELETE FROM sessions WHERE user_id='admin'")
    print("管理员账户已设置，现有商品与淘宝会话保持不变。")


if __name__ == "__main__":
    main()
