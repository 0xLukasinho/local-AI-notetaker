"""Register the Meet watcher with Windows Task Scheduler.

``meeting-notes autostart install`` creates one per-user task (no admin). The
task runs ``meeting-notes watch --ensure`` at logon, on unlock, every five
minutes, and on resume-from-sleep when Windows accepts that trigger. The task
is hidden and does not open a console.

An old Startup-folder ``MeetingNotesWatch.vbs`` is removed. Install does not
create a new one: a shortcut that only runs at login is what left the watcher
down all day with nobody noticing. If schtasks fails, install raises and the
error is logged.

The XML is built from the interpreter that runs install. Nothing in this
module hardcodes a user name, a home directory, or a webhook.
"""

import json
import os
import sys
from xml.sax.saxutils import escape

TASK_NAME = "MeetingNotesWatch"
AUTOSTART_FILENAME = "autostart.json"
TASK_XML_FILENAME = "MeetingNotesWatch.xml"


class AutostartError(RuntimeError):
    """The scheduled task could not be registered."""


def find_pythonw(executable=None):
    """Prefer pythonw.exe next to this interpreter so login starts headless."""
    exe = os.path.abspath(executable or sys.executable)
    base = os.path.basename(exe).lower()
    directory = os.path.dirname(exe)
    if base in ("python.exe", "pythonw.exe"):
        candidate = os.path.join(directory, "pythonw.exe")
        if os.path.isfile(candidate):
            return candidate
    return exe


def watcher_module_args():
    return ["-m", "meeting_notes.cli", "watch", "--ensure"]


def ensure_command_line(pythonw):
    """Command line recorded in autostart.json. The task itself uses XML."""
    return f'"{pythonw}" -m meeting_notes.cli watch --ensure'


def schtasks_register_argv(xml_path, task_name=TASK_NAME):
    return ["schtasks", "/Create", "/F", "/TN", task_name, "/XML", xml_path]


def schtasks_query_argv(task_name=TASK_NAME):
    return ["schtasks", "/Query", "/TN", task_name]


def schtasks_delete_argv(task_name=TASK_NAME):
    return ["schtasks", "/Delete", "/TN", task_name, "/F"]


def startup_vbs(pythonw):
    """Legacy Startup-folder script. Install no longer writes this.

    Uninstall still deletes ``MeetingNotesWatch.vbs``. The text is kept so the
    old hidden launch (window style 0) stays obvious in tests.
    """
    safe = pythonw.replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run """{safe}"" -m meeting_notes.cli watch", 0, False\r\n'
    )


def repetition_start(now=None):
    """Local time one minute from now.

    Task Scheduler treats a daily trigger whose time-of-day has already passed
    as finished for today, so the 5-minute repetition would wait until tomorrow.
    A start a minute ahead makes the first check soon, then every 5 minutes for
    the next day, and the daily schedule repeats that window.
    """
    from datetime import datetime, timedelta

    moment = (now or datetime.now()).replace(microsecond=0) + timedelta(minutes=1)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def task_xml(pythonw, include_resume=True, repetition_start_at=None):
    """Task Scheduler 2.0 XML for a per-user, least-privilege, hidden task.

    The five-minute trigger is a daily schedule that repeats for one day. Its
    start is one minute ahead so the window begins today instead of tomorrow.
    Logon waits 15 seconds so the user profile is ready. Resume-from-sleep is
    a System log event and is omitted when ``include_resume`` is false.
    """
    command = escape(pythonw)
    arguments = escape("-m meeting_notes.cli watch --ensure")
    boundary = escape(repetition_start_at or repetition_start())
    resume = ""
    if include_resume:
        query = (
            "<QueryList><Query Id=\"0\" Path=\"System\"><Select Path=\"System\">"
            "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and "
            "(EventID=107 or EventID=507)]]"
            "</Select></Query></QueryList>"
        )
        resume = (
            "    <EventTrigger>\r\n"
            "      <Enabled>true</Enabled>\r\n"
            f"      <Subscription>{escape(query)}</Subscription>\r\n"
            "    </EventTrigger>\r\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        "  <RegistrationInfo>\r\n"
        "    <Description>Keep the meeting-notes watcher running. "
        "Runs watch --ensure at logon, on unlock, every 5 minutes, "
        "and on resume from sleep when that trigger is available.</Description>\r\n"
        "  </RegistrationInfo>\r\n"
        "  <Triggers>\r\n"
        "    <LogonTrigger>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        "      <Delay>PT15S</Delay>\r\n"
        "    </LogonTrigger>\r\n"
        "    <SessionStateChangeTrigger>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        "      <StateChange>SessionUnlock</StateChange>\r\n"
        "    </SessionStateChangeTrigger>\r\n"
        "    <CalendarTrigger>\r\n"
        "      <Repetition>\r\n"
        "        <Interval>PT5M</Interval>\r\n"
        "        <Duration>P1D</Duration>\r\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\r\n"
        "      </Repetition>\r\n"
        f"      <StartBoundary>{boundary}</StartBoundary>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        "      <ScheduleByDay>\r\n"
        "        <DaysInterval>1</DaysInterval>\r\n"
        "      </ScheduleByDay>\r\n"
        "    </CalendarTrigger>\r\n"
        f"{resume}"
        "  </Triggers>\r\n"
        "  <Principals>\r\n"
        '    <Principal id="Author">\r\n'
        "      <LogonType>InteractiveToken</LogonType>\r\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\r\n"
        "    </Principal>\r\n"
        "  </Principals>\r\n"
        "  <Settings>\r\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\r\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\r\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\r\n"
        "    <Enabled>true</Enabled>\r\n"
        "    <Hidden>true</Hidden>\r\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\r\n"
        "    <WakeToRun>false</WakeToRun>\r\n"
        "    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>\r\n"
        "    <Priority>7</Priority>\r\n"
        "  </Settings>\r\n"
        '  <Actions Context="Author">\r\n'
        "    <Exec>\r\n"
        f"      <Command>{command}</Command>\r\n"
        f"      <Arguments>{arguments}</Arguments>\r\n"
        "    </Exec>\r\n"
        "  </Actions>\r\n"
        "</Task>\r\n"
    )


def default_startup_dir():
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def task_xml_path(control_dir):
    return os.path.join(control_dir, TASK_XML_FILENAME)


def autostart_meta_path(control_dir):
    return os.path.join(control_dir, AUTOSTART_FILENAME)


def read_autostart(control_dir):
    path = autostart_meta_path(control_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def startup_shortcut_path(startup_dir):
    return os.path.join(startup_dir, f"{TASK_NAME}.vbs")


def legacy_startup_paths(control_dir, startup_dir):
    paths = []
    meta = read_autostart(control_dir) or {}
    if meta.get("path"):
        paths.append(meta["path"])
    if startup_dir:
        paths.append(startup_shortcut_path(startup_dir))
    unique = []
    for path in paths:
        if path and path not in unique:
            unique.append(path)
    return unique


def remove_legacy_startup(control_dir, startup_dir):
    """Delete a leftover Startup-folder shortcut. Returns the paths removed."""
    removed = []
    for path in legacy_startup_paths(control_dir, startup_dir):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    return removed


def _write_task_xml(path, xml_text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # Task Scheduler wants UTF-16. atomic_write_text writes UTF-8, so write
    # the bytes here and replace the destination in one step.
    tmp = path + ".partial"
    try:
        with open(tmp, "wb") as handle:
            handle.write(xml_text.encode("utf-16"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _run_error(exc):
    detail = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
    return " ".join(str(detail).split()) or exc.__class__.__name__


def install_autostart(
    control_dir,
    pythonw=None,
    startup_dir=None,
    run=None,
    task_name=TASK_NAME,
    include_resume=True,
    repetition_start_at=None,
):
    """Create or replace the per-user task. Removes a legacy Startup shortcut.

    Raises AutostartError when schtasks cannot register the task. A Startup
    shortcut is never created as a fallback.
    """
    import subprocess

    from meeting_notes.atomic_io import atomic_write_json

    pythonw = pythonw or find_pythonw()
    startup_dir = startup_dir if startup_dir is not None else default_startup_dir()
    run = run or subprocess.run
    os.makedirs(control_dir, exist_ok=True)
    xml_path = task_xml_path(control_dir)
    boundary = repetition_start_at or repetition_start()
    attempts = (True, False) if include_resume else (False,)
    last_error = None
    registered_resume = False
    for resume in attempts:
        _write_task_xml(
            xml_path,
            task_xml(pythonw, include_resume=resume, repetition_start_at=boundary),
        )
        try:
            run(
                schtasks_register_argv(xml_path, task_name=task_name),
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            last_error = _run_error(exc)
            continue
        registered_resume = bool(resume)
        break
    else:
        raise AutostartError(
            "Could not register the scheduled task "
            f"'{task_name}'. A Startup-folder shortcut was not created. "
            f"schtasks said: {last_error}"
        )

    removed = remove_legacy_startup(control_dir, startup_dir)
    triggers = ["logon", "unlock", "every-5-minutes"]
    if registered_resume:
        triggers.append("resume")
    meta = {
        "method": "schtasks",
        "task_name": task_name,
        "pythonw": pythonw,
        "command": ensure_command_line(pythonw),
        "triggers": triggers,
        "resume_trigger": registered_resume,
        "xml_path": xml_path,
        "removed_startup": removed,
    }
    if include_resume and not registered_resume:
        meta["resume_error"] = last_error or "resume trigger was rejected"
    atomic_write_json(autostart_meta_path(control_dir), meta)
    return meta


def uninstall_autostart(control_dir, startup_dir=None, run=None, task_name=TASK_NAME):
    """Remove the scheduled task and any Startup script. Returns what was removed."""
    import subprocess

    run = run or subprocess.run
    startup_dir = startup_dir if startup_dir is not None else default_startup_dir()
    removed = []
    meta = read_autostart(control_dir) or {}
    name = meta.get("task_name") or task_name
    try:
        run(schtasks_delete_argv(name), check=True, capture_output=True, text=True)
        removed.append(f"schtasks:{name}")
    except Exception:
        pass
    for path in legacy_startup_paths(control_dir, startup_dir):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    for path in (meta.get("xml_path"), task_xml_path(control_dir)):
        if path and os.path.isfile(path) and path not in removed:
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    meta_path = autostart_meta_path(control_dir)
    if os.path.isfile(meta_path):
        try:
            os.remove(meta_path)
            removed.append(meta_path)
        except OSError:
            pass
    return removed


def autostart_status(control_dir, query_task=None, task_name=TASK_NAME):
    """Return a status dict. `query_task(task_name) -> bool` checks the scheduler."""
    meta = read_autostart(control_dir)
    installed = False
    method = None
    detail = "not installed"
    if meta:
        method = meta.get("method")
        if method == "schtasks":
            name = meta.get("task_name") or task_name
            if query_task is None:
                installed = _query_schtasks(name)
            else:
                installed = bool(query_task(name))
            triggers = meta.get("triggers") or []
            trigger_text = ", ".join(triggers) if triggers else "logon"
            if installed:
                detail = f"Task Scheduler ({name}): {trigger_text}"
            else:
                detail = f"task missing ({name})"
        elif method == "startup":
            path = meta.get("path") or ""
            installed = bool(path) and os.path.isfile(path)
            detail = path if installed else "startup script missing"
        else:
            detail = "unknown autostart method"
    return {
        "installed": installed,
        "method": method,
        "detail": detail,
        "meta": meta,
    }


def _query_schtasks(task_name):
    import subprocess

    if sys.platform != "win32":
        return False
    try:
        subprocess.run(
            schtasks_query_argv(task_name),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except Exception:
        return False
