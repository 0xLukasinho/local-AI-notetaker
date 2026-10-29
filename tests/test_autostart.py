import json
import os

import pytest

import meeting_notes.cli as cli
from meeting_notes.autostart import (
    find_pythonw,
    install_autostart,
    schtasks_create_argv,
    startup_vbs,
    uninstall_autostart,
    autostart_status,
)


def test_find_pythonw_prefers_sibling(tmp_path):
    exe = tmp_path / "python.exe"
    windowless = tmp_path / "pythonw.exe"
    exe.write_text("", encoding="utf-8")
    windowless.write_text("", encoding="utf-8")
    assert find_pythonw(str(exe)) == os.path.abspath(str(windowless))
    lonely = tmp_path / "other"
    lonely.mkdir()
    other = lonely / "python.exe"
    other.write_text("", encoding="utf-8")
    assert find_pythonw(str(other)) == os.path.abspath(str(other))


def test_schtasks_command_uses_pythonw_and_onlogon():
    argv = schtasks_create_argv(r"C:\Python\pythonw.exe")
    assert argv[:3] == ["schtasks", "/Create", "/F"]
    assert "ONLOGON" in argv
    command = argv[argv.index("/TR") + 1]
    assert command == r'"C:\Python\pythonw.exe" -m meeting_notes.cli watch'


def test_startup_vbs_is_hidden():
    text = startup_vbs(r"C:\Program Files\Python\pythonw.exe")
    assert 'WScript.Shell' in text
    assert 'pythonw.exe' in text
    assert ", 0, False" in text


def test_install_prefers_task_scheduler(tmp_path):
    calls = []

    def run(argv, check, capture_output, text):
        calls.append(argv)
        return None

    meta = install_autostart(
        str(tmp_path / "control"),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(tmp_path / "startup"),
        run=run,
    )
    assert meta["method"] == "schtasks"
    assert calls and "ONLOGON" in calls[0]
    saved = json.loads((tmp_path / "control" / "autostart.json").read_text(encoding="utf-8"))
    assert saved["method"] == "schtasks"
    assert "pythonw.exe" in saved["command"]


def test_install_falls_back_to_startup_script(tmp_path):
    def run(_argv, check, capture_output, text):
        raise OSError("schtasks unavailable")

    startup = tmp_path / "startup"
    meta = install_autostart(
        str(tmp_path / "control"),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(startup),
        run=run,
    )
    assert meta["method"] == "startup"
    script = startup / "MeetingNotesWatch.vbs"
    assert script.is_file()
    assert "pythonw.exe" in script.read_text(encoding="utf-8")
    status = autostart_status(str(tmp_path / "control"))
    assert status["installed"] is True
    assert status["method"] == "startup"


def test_uninstall_removes_startup_script(tmp_path):
    def run(_argv, check, capture_output, text):
        raise OSError("no schtasks")

    control = tmp_path / "control"
    startup = tmp_path / "startup"
    install_autostart(str(control), pythonw=r"C:\Python\pythonw.exe", startup_dir=str(startup), run=run)
    removed = uninstall_autostart(str(control), startup_dir=str(startup), run=run)
    assert any(item.endswith(".vbs") for item in removed)
    assert not (startup / "MeetingNotesWatch.vbs").exists()
    assert autostart_status(str(control))["installed"] is False


def test_watch_status_and_linux_guards(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path))
    cli.cmd_watch(cli.build_parser().parse_args(["watch", "--status"]))
    out = capsys.readouterr().out
    assert "Autostart: not installed" in out
    assert "Watcher process: not running" in out

    with pytest.raises(SystemExit) as install_exit:
        cli.cmd_watch(cli.build_parser().parse_args(["watch", "--install"]))
    assert install_exit.value.code == 1
    assert "Windows only" in capsys.readouterr().err

    with pytest.raises(SystemExit) as run_exit:
        cli.cmd_watch(cli.build_parser().parse_args(["watch"]))
    assert run_exit.value.code == 1
    assert "requires Windows" in capsys.readouterr().err

    with pytest.raises(SystemExit) as both:
        cli.cmd_watch(cli.build_parser().parse_args(["watch", "--install", "--status"]))
    assert both.value.code == 2
