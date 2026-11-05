"""Register the call watcher with Windows Task Scheduler.

``meeting-notes autostart install`` creates one per-user task (no admin). The
task runs ``meeting-notes watch --ensure`` at system startup when that trigger
is allowed, at logon, on unlock, and on resume-from-sleep when Windows accepts
that trigger. There is no repeating timer. The task is hidden and does not
open a console.

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


def _is_app_exec_alias(path):
    """True for the per-user WindowsApps app-execution alias.

    ``Win32_Process.Create`` cannot launch that alias (ReturnValue 8). The real
    Store interpreter lives under ``Program Files\\WindowsApps`` and is a
    normal executable, so that directory is not an alias.
    """
    if not path:
        return False
    parts = [part.casefold() for part in os.path.normpath(str(path)).split(os.sep)]
    for index in range(len(parts) - 1):
        if parts[index] == "microsoft" and parts[index + 1] == "windowsapps":
            return True
    return False


def _first_real_interpreter(paths):
    for item in paths:
        if not item:
            continue
        path = os.path.abspath(item)
        if os.path.isfile(path) and not _is_app_exec_alias(path):
            return path
    return None


def _resolve_real_interpreter():
    """An interpreter path CreateProcess and WMI can launch.

    Prefer ``sys.executable`` when it is a real file (a venv keeps its own
    interpreter). Microsoft Store Python reports the WindowsApps alias there;
    ``sys._base_executable`` or ``python.exe`` under ``sys.base_prefix`` is
    the real binary in that case.
    """
    executable = getattr(sys, "executable", None)
    base_executable = getattr(sys, "_base_executable", None)
    real = _first_real_interpreter((executable, base_executable))
    if real:
        return real
    prefix = getattr(sys, "base_prefix", None) or ""
    if prefix:
        real = _first_real_interpreter(
            (
                os.path.join(prefix, "python.exe"),
                os.path.join(prefix, "pythonw.exe"),
            )
        )
        if real:
            return real
    fallback = executable or base_executable or sys.executable
    return os.path.abspath(fallback)


def _pythonw_beside(exe):
    """``pythonw.exe`` next to ``exe`` when that file is a real interpreter."""
    exe = os.path.abspath(exe)
    base = os.path.basename(exe).lower()
    directory = os.path.dirname(exe)
    if base in ("python.exe", "pythonw.exe"):
        candidate = os.path.join(directory, "pythonw.exe")
        if os.path.isfile(candidate) and not _is_app_exec_alias(candidate):
            return candidate
    return exe


def find_pythonw(executable=None):
    """Prefer pythonw.exe next to a real interpreter so login starts headless.

    An explicit path is used as given. With no path, Store app-execution
    aliases under ``WindowsApps`` are replaced by ``sys._base_executable`` or
    by ``pythonw.exe`` next to the real ``python.exe``.
    """
    if executable and not _is_app_exec_alias(executable):
        return _pythonw_beside(executable)
    return _pythonw_beside(_resolve_real_interpreter())


def _windows_elevation():
    """True when this process is elevated, False when it is not, None off Windows."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def _is_access_denied(detail):
    text = str(detail or "").lower()
    return "access is denied" in text or "access denied" in text


# Omitted ``elevated`` asks Windows. None means unknown, so startup is attempted.
_DETECT_ELEVATION = object()


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


def registration_plans(include_boot=True, include_resume=True):
    """Most complete trigger set first.

    A boot trigger often needs an administrator. Resume-from-sleep reads the
    System event log and is sometimes rejected for a per-user task. Logon and
    unlock are always in the XML. Later plans drop the optional triggers.
    """
    candidates = []
    if include_boot and include_resume:
        candidates.append((True, True))
    if include_resume:
        candidates.append((False, True))
    if include_boot:
        candidates.append((True, False))
    candidates.append((False, False))
    plans = []
    seen = set()
    for boot, resume in candidates:
        if (boot, resume) in seen:
            continue
        seen.add((boot, resume))
        plans.append({"boot": boot, "resume": resume})
    return plans


def task_xml(pythonw, include_resume=True, include_boot=True):
    """Task Scheduler 2.0 XML for a per-user, least-privilege, hidden task.

    Logon waits 15 seconds so the profile is ready. The boot trigger waits 30
    seconds so devices can settle, and is omitted when ``include_boot`` is
    false. Resume-from-sleep is a System log event and is omitted when
    ``include_resume`` is false. There is no repeating trigger.
    """
    command = escape(pythonw)
    arguments = escape("-m meeting_notes.cli watch --ensure")
    boot = ""
    if include_boot:
        boot = (
            "    <BootTrigger>\r\n"
            "      <Enabled>true</Enabled>\r\n"
            "      <Delay>PT30S</Delay>\r\n"
            "    </BootTrigger>\r\n"
        )
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
        "Runs watch --ensure at system startup, at logon, on unlock, "
        "and on resume from sleep when those triggers are available.</Description>\r\n"
        "  </RegistrationInfo>\r\n"
        "  <Triggers>\r\n"
        f"{boot}"
        "    <LogonTrigger>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        "      <Delay>PT15S</Delay>\r\n"
        "    </LogonTrigger>\r\n"
        "    <SessionStateChangeTrigger>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        "      <StateChange>SessionUnlock</StateChange>\r\n"
        "    </SessionStateChangeTrigger>\r\n"
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
    include_boot=True,
    elevated=_DETECT_ELEVATION,
):
    """Create or replace the per-user task. Removes a legacy Startup shortcut.

    Raises AutostartError when schtasks cannot register the task. A Startup
    shortcut is never created as a fallback. If a boot trigger is rejected,
    install retries with logon in its place. A shell that is not elevated
    skips the startup trigger and registers logon, unlock, and resume.
    """
    import subprocess

    from meeting_notes.atomic_io import atomic_write_json

    pythonw = pythonw or find_pythonw()
    startup_dir = startup_dir if startup_dir is not None else default_startup_dir()
    run = run or subprocess.run
    if elevated is _DETECT_ELEVATION:
        elevated = _windows_elevation()
    # False: this PowerShell is not an Administrator, so do not ask schtasks
    # for the startup trigger (that call returns "Access is denied").
    # None: elevation is unknown, so try startup and drop it if Windows rejects it.
    try_boot = bool(include_boot) and elevated is not False
    os.makedirs(control_dir, exist_ok=True)
    xml_path = task_xml_path(control_dir)
    plans = registration_plans(include_boot=try_boot, include_resume=include_resume)
    last_error = None
    chosen = None
    for plan in plans:
        _write_task_xml(
            xml_path,
            task_xml(
                pythonw,
                include_resume=plan["resume"],
                include_boot=plan["boot"],
            ),
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
        chosen = plan
        break
    else:
        message = (
            "Could not register the scheduled task "
            f"'{task_name}'. A Startup-folder shortcut was not created. "
            f"schtasks said: {last_error}"
        )
        if _is_access_denied(last_error):
            message += (
                " Open an Administrator PowerShell and rerun "
                "`meeting-notes autostart install`."
            )
        raise AutostartError(message)

    removed = remove_legacy_startup(control_dir, startup_dir)
    triggers = ["logon", "unlock"]
    if chosen["boot"]:
        triggers.insert(0, "startup")
    if chosen["resume"]:
        triggers.append("resume")
    meta = {
        "method": "schtasks",
        "task_name": task_name,
        "pythonw": pythonw,
        "command": ensure_command_line(pythonw),
        "triggers": triggers,
        "startup_trigger": bool(chosen["boot"]),
        "resume_trigger": bool(chosen["resume"]),
        "xml_path": xml_path,
        "removed_startup": removed,
    }
    if include_boot and not chosen["boot"]:
        if elevated is False:
            meta["startup_error"] = (
                "System startup was skipped because this shell is not an "
                "Administrator PowerShell. The task was registered without the "
                "startup trigger. Rerun `meeting-notes autostart install` from an "
                "Administrator PowerShell to add it."
            )
        else:
            meta["startup_error"] = last_error or "startup trigger was rejected"
        meta["startup_skipped"] = True
    if include_resume and not chosen["resume"]:
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
