"""Register the Meet watcher to start at Windows login.

Task Scheduler is preferred. If schtasks fails, a Startup-folder VBScript
launches pythonw with window style 0 (no console). Both paths are described
by pure helpers so tests can run without Windows.
"""

import json
import os
import sys

from meeting_notes.atomic_io import atomic_write_json

TASK_NAME = "MeetingNotesWatch"
AUTOSTART_FILENAME = "autostart.json"


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
    return ["-m", "meeting_notes.cli", "watch"]


def schtasks_create_argv(pythonw, task_name=TASK_NAME):
    command = subprocess_command_line(pythonw)
    return [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        task_name,
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TR",
        command,
    ]


def schtasks_query_argv(task_name=TASK_NAME):
    return ["schtasks", "/Query", "/TN", task_name]


def schtasks_delete_argv(task_name=TASK_NAME):
    return ["schtasks", "/Delete", "/TN", task_name, "/F"]


def subprocess_command_line(pythonw):
    """A single command line suitable for schtasks /TR and for the VBS runner."""
    return f'"{pythonw}" -m meeting_notes.cli watch'


def startup_vbs(pythonw):
    """VBScript that runs the watcher with no window (style 0)."""
    safe = pythonw.replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run """{safe}"" -m meeting_notes.cli watch", 0, False\r\n'
    )


def default_startup_dir():
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


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


def install_autostart(control_dir, pythonw=None, startup_dir=None, run=None, task_name=TASK_NAME):
    """Create the login task. Falls back to a Startup VBS if schtasks fails.

    Returns a small dict describing what was installed.
    """
    import subprocess

    pythonw = pythonw or find_pythonw()
    startup_dir = startup_dir or default_startup_dir()
    run = run or subprocess.run
    os.makedirs(control_dir, exist_ok=True)
    argv = schtasks_create_argv(pythonw, task_name=task_name)
    try:
        run(argv, check=True, capture_output=True, text=True)
    except Exception as exc:
        vbs_path = _install_startup_vbs(startup_dir, pythonw)
        meta = {
            "method": "startup",
            "path": vbs_path,
            "pythonw": pythonw,
            "task_name": task_name,
            "fallback_reason": str(exc),
        }
    else:
        meta = {
            "method": "schtasks",
            "task_name": task_name,
            "pythonw": pythonw,
            "command": subprocess_command_line(pythonw),
        }
    atomic_write_json(autostart_meta_path(control_dir), meta)
    return meta


def _install_startup_vbs(startup_dir, pythonw):
    os.makedirs(startup_dir, exist_ok=True)
    path = os.path.join(startup_dir, f"{TASK_NAME}.vbs")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(startup_vbs(pythonw))
    return path


def uninstall_autostart(control_dir, startup_dir=None, run=None, task_name=TASK_NAME):
    """Remove the scheduled task and any Startup script. Returns what was removed."""
    import subprocess

    run = run or subprocess.run
    startup_dir = startup_dir or default_startup_dir()
    removed = []
    meta = read_autostart(control_dir) or {}
    name = meta.get("task_name") or task_name
    try:
        run(schtasks_delete_argv(name), check=True, capture_output=True, text=True)
        removed.append(f"schtasks:{name}")
    except Exception:
        pass
    candidates = []
    if meta.get("path"):
        candidates.append(meta["path"])
    candidates.append(os.path.join(startup_dir, f"{TASK_NAME}.vbs"))
    for path in candidates:
        if path and os.path.isfile(path):
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
            detail = f"Task Scheduler ({name})" if installed else f"task missing ({name})"
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
