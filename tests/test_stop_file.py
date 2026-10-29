import os
import subprocess
import sys
import threading

import pytest

import meeting_notes.cli as cli
from meeting_notes.session import (
    read_active,
    request_stop,
    stop_requested,
    write_active,
    write_session_stop,
    write_stop_request,
)


def test_wait_for_stop_notices_a_stop_file(tmp_path):
    event = threading.Event()
    stop_file = tmp_path / "STOP"

    def should_stop():
        return stop_file.exists()

    def create_later():
        stop_file.write_text("stop\n", encoding="utf-8")

    timer = threading.Timer(0.05, create_later)
    timer.start()
    cli._wait_for_stop(event, should_stop=should_stop, poll_timeout=0.02)
    timer.cancel()
    assert event.is_set()


def test_stop_request_matches_pid_or_session_folder_only(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    control = tmp_path / "control"
    control.mkdir()
    assert stop_requested(str(folder), str(control), pid=10) is False

    write_stop_request(str(control), pid=99, folder="/other/session")
    assert stop_requested(str(folder), str(control), pid=10) is False

    write_stop_request(str(control), pid=10, folder="/other/session")
    assert stop_requested(str(folder), str(control), pid=10) is True

    write_stop_request(str(control), pid=99, folder=str(folder))
    assert stop_requested(str(folder), str(control), pid=10) is True

    os.remove(os.path.join(str(control), "stop-request.json"))
    write_session_stop(str(folder))
    assert stop_requested(str(folder), str(control), pid=10) is True


def test_request_stop_writes_both_signals_without_ctrl_break(tmp_path):
    folder = tmp_path / "session"
    control = tmp_path / "control"
    sent = request_stop(str(control), str(folder), pid=1234, break_ok=True)
    assert sent is False  # not Windows, or no console group to signal
    assert (folder / "STOP").read_text(encoding="utf-8") == "stop\n"
    assert stop_requested(str(folder), str(control), pid=1234) is True


def _sleep_process():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_stop_command_signals_the_live_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path))
    proc = _sleep_process()
    try:
        folder = tmp_path / "2026-10-29_standup"
        folder.mkdir()
        write_active(
            str(tmp_path),
            {
                "pid": proc.pid,
                "folder": str(folder),
                "mode": "manual",
                "break_ok": False,
            },
        )
        cli.cmd_stop(None)
    finally:
        proc.kill()
        proc.wait(timeout=5)
    out = capsys.readouterr().out
    assert "Stop requested" in out
    assert "stop file" in out
    assert (folder / "STOP").is_file()


def test_stop_command_when_nothing_is_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path))
    proc = _sleep_process()
    proc.kill()
    proc.wait(timeout=5)
    write_active(str(tmp_path), {"pid": proc.pid, "folder": str(tmp_path), "mode": "manual"})
    with pytest.raises(SystemExit) as exc:
        cli.cmd_stop(None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "No recording session is running." in err
    assert read_active(str(tmp_path)) is None
