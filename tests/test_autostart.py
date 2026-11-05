import json
import os

import pytest

import meeting_notes.cli as cli
from meeting_notes.autostart import (
    AutostartError,
    autostart_status,
    ensure_command_line,
    find_pythonw,
    install_autostart,
    startup_vbs,
    task_xml,
    uninstall_autostart,
)


def _ok_run(_argv, check, capture_output, text):
    return None


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


def test_task_xml_is_hidden_and_repeats_ensure():
    pythonw = r"C:\Python\pythonw.exe"
    xml = task_xml(pythonw, include_resume=True)
    assert "<?xml" in xml
    assert "<LogonTrigger>" in xml
    assert "<Delay>PT15S</Delay>" in xml
    assert "<StateChange>SessionUnlock</StateChange>" in xml
    assert "<Interval>PT5M</Interval>" in xml
    assert "<DaysInterval>1</DaysInterval>" in xml
    assert "EventID=107" in xml
    assert "EventID=507" in xml
    assert "<Hidden>true</Hidden>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<Priority>7</Priority>" in xml
    assert "<UserId>" not in xml
    assert "webhook" not in xml.lower()
    assert pythonw in xml
    assert "-m meeting_notes.cli watch --ensure" in xml
    assert "<StartBoundary>2026-11-05T09:00:00</StartBoundary>" in task_xml(
        pythonw,
        include_resume=False,
        repetition_start_at="2026-11-05T09:00:00",
    )
    assert "cmd.exe" not in xml.lower()
    assert ensure_command_line(pythonw) == f'"{pythonw}" -m meeting_notes.cli watch --ensure'


def test_task_xml_escapes_the_interpreter_path():
    xml = task_xml(r"C:\A&B\pythonw.exe", include_resume=False)
    assert r"C:\A&amp;B\pythonw.exe" in xml
    assert "Kernel-Power" not in xml
    assert "PT5M" in xml


def test_startup_vbs_is_hidden():
    text = startup_vbs(r"C:\Program Files\Python\pythonw.exe")
    assert "WScript.Shell" in text
    assert "pythonw.exe" in text
    assert ", 0, False" in text


def test_install_registers_task_and_removes_startup_shortcut(tmp_path):
    calls = []

    def run(argv, check, capture_output, text):
        calls.append(argv)
        return None

    startup = tmp_path / "startup"
    startup.mkdir()
    shortcut = startup / "MeetingNotesWatch.vbs"
    shortcut.write_text("old shortcut", encoding="utf-8")
    meta = install_autostart(
        str(tmp_path / "control"),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(startup),
        run=run,
    )
    assert meta["method"] == "schtasks"
    assert meta["resume_trigger"] is True
    assert "every-5-minutes" in meta["triggers"]
    assert "logon" in meta["triggers"]
    assert "unlock" in meta["triggers"]
    assert "resume" in meta["triggers"]
    assert calls and calls[0][1:5] == ["/Create", "/F", "/TN", "MeetingNotesWatch"]
    assert "/XML" in calls[0]
    assert not shortcut.exists()
    assert meta["removed_startup"] == [str(shortcut)]
    xml_path = tmp_path / "control" / "MeetingNotesWatch.xml"
    xml = xml_path.read_text(encoding="utf-16")
    assert "watch --ensure" in xml
    assert "PT5M" in xml
    saved = json.loads((tmp_path / "control" / "autostart.json").read_text(encoding="utf-8"))
    assert saved["command"].endswith("watch --ensure")
    status = autostart_status(str(tmp_path / "control"), query_task=lambda _name: True)
    assert status["installed"] is True
    assert "every-5-minutes" in status["detail"]


def test_install_retries_without_the_resume_trigger(tmp_path):
    calls = []

    def run(_argv, check, capture_output, text):
        calls.append(_argv)
        if len(calls) == 1:
            raise OSError("event trigger denied")
        return None

    meta = install_autostart(
        str(tmp_path / "control"),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(tmp_path / "startup"),
        run=run,
    )
    assert len(calls) == 2
    assert meta["resume_trigger"] is False
    assert "resume" not in meta["triggers"]
    assert "unlock" in meta["triggers"]
    xml = (tmp_path / "control" / "MeetingNotesWatch.xml").read_text(encoding="utf-16")
    assert "Kernel-Power" not in xml
    assert "PT5M" in xml
    assert "SessionUnlock" in xml


def test_install_does_not_fall_back_to_a_startup_script(tmp_path):
    def run(_argv, check, capture_output, text):
        raise OSError("schtasks unavailable")

    startup = tmp_path / "startup"
    with pytest.raises(AutostartError) as exc:
        install_autostart(
            str(tmp_path / "control"),
            pythonw=r"C:\Python\pythonw.exe",
            startup_dir=str(startup),
            run=run,
        )
    assert "Startup-folder shortcut was not created" in str(exc.value)
    assert "schtasks unavailable" in str(exc.value)
    assert not (startup / "MeetingNotesWatch.vbs").exists()
    assert autostart_status(str(tmp_path / "control"))["installed"] is False


def test_uninstall_removes_task_xml_and_startup_script(tmp_path):
    control = tmp_path / "control"
    startup = tmp_path / "startup"
    install_autostart(
        str(control),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(startup),
        run=_ok_run,
    )
    shortcut = startup / "MeetingNotesWatch.vbs"
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    shortcut.write_text("leftover", encoding="utf-8")
    removed = uninstall_autostart(str(control), startup_dir=str(startup), run=_ok_run)
    assert any(item.startswith("schtasks:") for item in removed)
    assert any(item.endswith(".vbs") for item in removed)
    assert any(item.endswith(".xml") for item in removed)
    assert not shortcut.exists()
    assert not (control / "MeetingNotesWatch.xml").exists()
    assert autostart_status(str(control))["installed"] is False


def test_watch_status_and_linux_guards(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CONTROL_DIR", str(tmp_path))
    cli.cmd_watch(cli.build_parser().parse_args(["watch", "--status"]))
    out = capsys.readouterr().out
    assert "Autostart: not installed" in out
    assert "Watcher process: not running" in out
    assert "Heartbeat: missing" in out

    with pytest.raises(SystemExit) as install_exit:
        cli.cmd_watch(cli.build_parser().parse_args(["watch", "--install"]))
    assert install_exit.value.code == 1
    assert "Windows only" in capsys.readouterr().err

    with pytest.raises(SystemExit) as ensure_exit:
        cli.cmd_watch(cli.build_parser().parse_args(["watch", "--ensure"]))
    assert ensure_exit.value.code == 1
    assert "requires Windows" in capsys.readouterr().err

    with pytest.raises(SystemExit) as run_exit:
        cli.cmd_watch(cli.build_parser().parse_args(["watch"]))
    assert run_exit.value.code == 1
    assert "requires Windows" in capsys.readouterr().err

    with pytest.raises(SystemExit) as both:
        cli.cmd_watch(cli.build_parser().parse_args(["watch", "--install", "--status"]))
    assert both.value.code == 2

    with pytest.raises(SystemExit) as autostart_install:
        cli.cmd_autostart(cli.build_parser().parse_args(["autostart", "install"]))
    assert autostart_install.value.code == 1
    assert "Windows only" in capsys.readouterr().err

    cli.cmd_autostart(cli.build_parser().parse_args(["autostart", "status"]))
    assert "Heartbeat: missing" in capsys.readouterr().out
