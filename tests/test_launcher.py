import json
import signal
from types import SimpleNamespace

import run


def test_macos_resolved_python_executable_is_identified_by_cwd(monkeypatch):
    def command(args, **kwargs):
        if args[0] == "/bin/ps":
            return SimpleNamespace(
                stdout="/Library/Frameworks/Python.app/Python -m uvicorn backend.app:app"
            )
        return SimpleNamespace(stdout=f"p123\nn{run.ROOT}\n")

    monkeypatch.setattr(run.subprocess, "run", command)
    assert run.owns_service(123)


def test_launcher_never_stops_another_project(monkeypatch):
    def command(args, **kwargs):
        return SimpleNamespace(
            stdout=(
                "python -m uvicorn backend.app:app"
                if args[0] == "/bin/ps"
                else "p123\nn/tmp/unrelated-project\n"
            )
        )

    monkeypatch.setattr(run.subprocess, "run", command)
    assert not run.owns_service(123)


def test_stop_waits_for_its_service_to_exit(tmp_path, monkeypatch):
    metadata = tmp_path / "service.json"
    metadata.write_text(json.dumps({"pid": 123}))
    monkeypatch.setattr(run, "DATA", tmp_path)
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--stop"])
    monkeypatch.setattr(run, "owns_service", lambda pid: True)
    states = iter([True, False, False])
    monkeypatch.setattr(run, "pid_running", lambda pid: next(states))
    monkeypatch.setattr(run.time, "sleep", lambda _: None)
    sent = []
    monkeypatch.setattr(run.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert run.main() == 0
    assert sent == [(123, signal.SIGTERM)]
    assert not metadata.exists()


def test_mismatched_live_pid_is_preserved(tmp_path, monkeypatch):
    metadata = tmp_path / "service.json"
    metadata.write_text(json.dumps({"pid": 123}))
    monkeypatch.setattr(run, "DATA", tmp_path)
    monkeypatch.setattr(run.sys, "argv", ["run.py", "--stop"])
    monkeypatch.setattr(run, "owns_service", lambda pid: False)
    monkeypatch.setattr(run, "pid_running", lambda pid: True)
    monkeypatch.setattr(
        run.os,
        "kill",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not kill")),
    )
    assert run.main() == 1
    assert metadata.exists()
