import json
import os
import sys

import pytest

import meeting_notes.cli as cli
from meeting_notes.autostart import (
    AutostartError,
    autostart_status,
    ensure_command_line,
    find_pythonw,
    install_autostart,
    registration_plans,
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


def _touch(path, body=b"MZ"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def test_find_pythonw_skips_the_windowsapps_alias(tmp_path, monkeypatch):
    alias_dir = (
        tmp_path
        / "AppData"
        / "Local"
        / "Microsoft"
        / "WindowsApps"
        / "PythonSoftwareFoundation.Python.3.10_qbz5n2kfra8p0"
    )
    _touch(alias_dir / "python.exe", b"")
    _touch(alias_dir / "pythonw.exe", b"")
    real_dir = tmp_path / "Program Files" / "WindowsApps" / "PythonSoftwareFoundation.Python.3.10_x64"
    _touch(real_dir / "python.exe")
    _touch(real_dir / "pythonw.exe")
    monkeypatch.setattr(sys, "executable", str(alias_dir / "python.exe"))
    monkeypatch.setattr(sys, "_base_executable", str(real_dir / "python.exe"), raising=False)
    assert find_pythonw() == str(real_dir / "pythonw.exe")


def test_find_pythonw_uses_base_prefix_when_executable_and_base_are_aliases(tmp_path, monkeypatch):
    alias_dir = tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    _touch(alias_dir / "python.exe", b"")
    _touch(alias_dir / "pythonw.exe", b"")
    prefix = tmp_path / "LocalCache" / "Python310"
    _touch(prefix / "python.exe")
    _touch(prefix / "pythonw.exe")
    monkeypatch.setattr(sys, "executable", str(alias_dir / "python.exe"))
    monkeypatch.setattr(sys, "_base_executable", str(alias_dir / "python.exe"), raising=False)
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    assert find_pythonw() == str(prefix / "pythonw.exe")


def test_find_pythonw_keeps_a_venv_interpreter(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    base = tmp_path / "base"
    _touch(venv / "python.exe")
    _touch(venv / "pythonw.exe")
    _touch(base / "python.exe")
    _touch(base / "pythonw.exe")
    monkeypatch.setattr(sys, "executable", str(venv / "python.exe"))
    monkeypatch.setattr(sys, "_base_executable", str(base / "python.exe"), raising=False)
    monkeypatch.setattr(sys, "base_prefix", str(base))
    assert find_pythonw() == str(venv / "pythonw.exe")


def test_task_xml_runs_ensure_at_startup_logon_unlock_and_resume():
    pythonw = r"C:\Python\pythonw.exe"
    xml = task_xml(pythonw, include_resume=True, include_boot=True)
    assert "<?xml" in xml
    assert "<BootTrigger>" in xml
    assert "<Delay>PT30S</Delay>" in xml
    assert "<LogonTrigger>" in xml
    assert "<Delay>PT15S</Delay>" in xml
    assert "<StateChange>SessionUnlock</StateChange>" in xml
    assert "EventID=107" in xml
    assert "EventID=507" in xml
    assert "<CalendarTrigger>" not in xml
    assert "PT5M" not in xml
    assert "<Repetition>" not in xml
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
    assert "cmd.exe" not in xml.lower()
    assert ensure_command_line(pythonw) == f'"{pythonw}" -m meeting_notes.cli watch --ensure'
    plans = registration_plans()
    assert plans[0] == {"boot": True, "resume": True}
    assert plans[1] == {"boot": False, "resume": True}
    assert {"boot": False, "resume": False} in plans


def test_task_xml_escapes_the_interpreter_path():
    xml = task_xml(r"C:\A&B\pythonw.exe", include_resume=False, include_boot=False)
    assert r"C:\A&amp;B\pythonw.exe" in xml
    assert "Kernel-Power" not in xml
    assert "<BootTrigger>" not in xml
    assert "PT5M" not in xml
    assert "<LogonTrigger>" in xml


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
        elevated=True,
    )
    assert meta["method"] == "schtasks"
    assert meta["resume_trigger"] is True
    assert meta["startup_trigger"] is True
    assert "every-5-minutes" not in meta["triggers"]
    assert meta["triggers"][0] == "startup"
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
    assert "<BootTrigger>" in xml
    assert "PT5M" not in xml
    saved = json.loads((tmp_path / "control" / "autostart.json").read_text(encoding="utf-8"))
    assert saved["command"].endswith("watch --ensure")
    status = autostart_status(str(tmp_path / "control"), query_task=lambda _name: True)
    assert status["installed"] is True
    assert "startup" in status["detail"]
    assert "every-5-minutes" not in status["detail"]


def test_install_drops_the_boot_trigger_when_startup_needs_admin(tmp_path):
    calls = []
    control = tmp_path / "control"

    def run(_argv, check, capture_output, text):
        calls.append(_argv)
        xml = (control / "MeetingNotesWatch.xml").read_text(encoding="utf-16")
        if "<BootTrigger>" in xml:
            raise OSError("boot trigger denied")
        return None

    meta = install_autostart(
        str(control),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(tmp_path / "startup"),
        run=run,
        elevated=True,
    )
    assert len(calls) == 2
    assert meta["startup_trigger"] is False
    assert meta["resume_trigger"] is True
    assert "startup" not in meta["triggers"]
    assert "logon" in meta["triggers"]
    assert "unlock" in meta["triggers"]
    assert "boot trigger denied" in meta["startup_error"]
    xml = (control / "MeetingNotesWatch.xml").read_text(encoding="utf-16")
    assert "<BootTrigger>" not in xml
    assert "Kernel-Power" in xml
    assert "SessionUnlock" in xml
    assert "PT5M" not in xml


def test_install_can_drop_boot_and_resume(tmp_path):
    calls = []
    control = tmp_path / "control"

    def run(_argv, check, capture_output, text):
        calls.append(_argv)
        xml = (control / "MeetingNotesWatch.xml").read_text(encoding="utf-16")
        if "Kernel-Power" in xml or "<BootTrigger>" in xml:
            raise OSError("optional trigger denied")
        return None

    meta = install_autostart(
        str(control),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(tmp_path / "startup"),
        run=run,
        elevated=True,
    )
    assert len(calls) == 4
    assert meta["startup_trigger"] is False
    assert meta["resume_trigger"] is False
    assert meta["triggers"] == ["logon", "unlock"]
    xml = (control / "MeetingNotesWatch.xml").read_text(encoding="utf-16")
    assert "<BootTrigger>" not in xml
    assert "Kernel-Power" not in xml
    assert "<LogonTrigger>" in xml
    assert "SessionUnlock" in xml


def test_install_skips_startup_when_the_shell_is_not_elevated(tmp_path):
    calls = []
    control = tmp_path / "control"

    def run(_argv, check, capture_output, text):
        calls.append((control / "MeetingNotesWatch.xml").read_text(encoding="utf-16"))
        return None

    meta = install_autostart(
        str(control),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(tmp_path / "startup"),
        run=run,
        elevated=False,
    )
    assert len(calls) == 1
    assert "<BootTrigger>" not in calls[0]
    assert "Kernel-Power" in calls[0]
    assert "SessionUnlock" in calls[0]
    assert meta["startup_trigger"] is False
    assert meta["startup_skipped"] is True
    assert meta["resume_trigger"] is True
    assert meta["triggers"] == ["logon", "unlock", "resume"]
    assert "skipped" in meta["startup_error"].lower()
    assert "Administrator PowerShell" in meta["startup_error"]


def test_install_falls_back_when_startup_returns_access_denied(tmp_path):
    calls = []
    control = tmp_path / "control"

    def run(_argv, check, capture_output, text):
        calls.append(True)
        xml = (control / "MeetingNotesWatch.xml").read_text(encoding="utf-16")
        if "<BootTrigger>" in xml:
            raise OSError("ERROR: Access is denied.")
        return None

    meta = install_autostart(
        str(control),
        pythonw=r"C:\Python\pythonw.exe",
        startup_dir=str(tmp_path / "startup"),
        run=run,
        elevated=None,
    )
    assert len(calls) == 2
    assert meta["startup_trigger"] is False
    assert "logon" in meta["triggers"]
    assert "unlock" in meta["triggers"]
    assert "resume" in meta["triggers"]
    assert "Access is denied" in meta["startup_error"]


def test_install_tells_the_user_to_open_an_administrator_powershell(tmp_path):
    def run(_argv, check, capture_output, text):
        raise OSError("ERROR: Access is denied.")

    with pytest.raises(AutostartError) as exc:
        install_autostart(
            str(tmp_path / "control"),
            pythonw=r"C:\Python\pythonw.exe",
            startup_dir=str(tmp_path / "startup"),
            run=run,
            elevated=True,
        )
    message = str(exc.value)
    assert "Access is denied" in message
    assert "Administrator PowerShell" in message
    assert "Startup-folder shortcut was not created" in message


def test_print_says_startup_was_skipped_without_the_raw_access_denied(capsys):
    cli._print_autostart_result(
        {
            "task_name": "MeetingNotesWatch",
            "startup_trigger": False,
            "resume_trigger": True,
            "startup_error": "ERROR: Access is denied.",
            "removed_startup": [],
        }
    )
    out = capsys.readouterr().out
    assert "startup was skipped" in out.lower() or "System startup was skipped" in out
    assert "Administrator PowerShell" in out
    assert "ERROR: Access is denied." not in out


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
            elevated=True,
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
        elevated=True,
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
