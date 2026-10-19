"""Start/stop the macOS local monitor; closing Terminal does not stop collection."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("MONITOR_DATA_DIR", ROOT / "data"))
URL = "http://127.0.0.1:8765"


def owns_service(pid):
    """macOS resolves Python's executable path, so verify the process cwd too."""
    process = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True
    )
    if "backend.app:app" not in process.stdout:
        return False
    cwd = subprocess.run(
        ["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        text=True,
        capture_output=True,
    )
    return any(
        line.startswith("n") and Path(line[1:]).resolve() == ROOT
        for line in cwd.stdout.splitlines()
    )


def pid_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def alive():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL + "/api/ping", timeout=1) as response:
            return json.load(response).get("ok") is True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    pidfile = DATA / "service.json"
    if args.stop:
        if not pidfile.exists():
            print("没有找到由启动脚本运行的服务。")
            return 0
        info = json.loads(pidfile.read_text())
        pid = info["pid"]
        if owns_service(pid):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while pid_running(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if pid_running(pid) and owns_service(pid):
                os.kill(pid, signal.SIGKILL)
            print("采集服务已停止。")
        elif pid_running(pid):
            print("记录的进程不属于本项目，未停止任何进程。")
            return 1
        else:
            print("服务已经停止。")
        pidfile.unlink(missing_ok=True)
        return 0
    if alive():
        print(f"服务已在运行：{URL}")
        if not args.no_browser:
            webbrowser.open(URL)
        return 0
    if not (ROOT / "dist" / "index.html").exists():
        print("正在构建前端…")
        subprocess.run(["npm", "run", "build"], cwd=ROOT, check=True)
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "backend.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--no-access-log",
        "--no-server-header",
        "--timeout-graceful-shutdown",
        "5",
    ]
    if args.foreground:
        os.chdir(ROOT)
        os.execv(sys.executable, command)
    log = DATA / "server.log"
    if log.exists() and log.stat().st_size > 5_000_000:
        log.replace(DATA / "server.previous.log")
    with log.open("ab") as output:
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=output, stderr=output, start_new_session=True
        )
    pidfile.write_text(json.dumps({"pid": process.pid, "url": URL}))
    pidfile.chmod(0o600)
    for _ in range(50):
        if alive():
            print(
                f"鬼打架已启动：{URL}\n关闭终端或网页后，后台继续运行。双击「停止监测.command」可停止。"
            )
            if not args.no_browser:
                webbrowser.open(URL)
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.2)
    print(f"启动失败，请查看 {log}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
