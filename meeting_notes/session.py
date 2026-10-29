"""Session folders, the active-session lock, stop files, and status.json.

Windows process checks and CTRL_BREAK live behind pid_alive / signal_ctrl_break
so the rest of this module runs on Linux.
"""

import json
import os
import sys
import time
from datetime import datetime

from meeting_notes.atomic_io import atomic_write_json

ACTIVE_FILENAME = "active.json"
STOP_REQUEST_FILENAME = "stop-request.json"
WATCHER_PID_FILENAME = "watcher.pid"
OUTBOX_DIRNAME = "outbox"
STOP_FILENAME = "STOP"
STATUS_FILENAME = "status.json"
LOG_FILENAME = "watch.log"

STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_DONE = "done"
STATE_FAILED = "failed"

# Keys the watcher may set before the recorder advances state. A later update
# must not blank them out.
PRESERVE_ON_UPDATE = ("left_at", "joined_at", "meet_code", "window_title", "delivered_at")


def slugify(text):
    """Convert text to a filesystem-safe slug."""
    import re

    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


def iso_now(moment=None):
    """ISO 8601 timestamp with a numeric UTC offset."""
    moment = moment or datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.isoformat(timespec="seconds")


def allocate_meeting_dir(output_dir, meeting_name, unique=False, when=None):
    """Create a session directory under output_dir.

    unique=False keeps the historical `YYYY-MM-DD_slug` name (exist_ok).
    unique=True appends `_HHMM` when that folder already exists, then
    `_HHMM_2`, `_HHMM_3`, ... so an automated session never reuses a folder.
    """
    when = when or datetime.now()
    today = when.date().isoformat()
    slug = slugify(meeting_name) or "meeting"
    base = f"{today}_{slug}"
    primary = os.path.join(output_dir, base)
    if not unique or not os.path.exists(primary):
        os.makedirs(primary, exist_ok=True)
        return primary
    stamped = f"{base}_{when.strftime('%H%M')}"
    candidate = os.path.join(output_dir, stamped)
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(output_dir, f"{stamped}_{suffix}")
        suffix += 1
    os.makedirs(candidate, exist_ok=True)
    return candidate


def transcript_path(folder):
    return os.path.join(folder, "transcript.txt")


def has_transcript(folder):
    """True when transcript.txt is already present.

    Any existing file counts, including an empty one, so a later session cannot
    clobber it. The writer publishes the file only when the text is complete.
    """
    return os.path.isfile(transcript_path(folder))


def status_path(folder):
    return os.path.join(folder, STATUS_FILENAME)


def read_json(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def read_status(folder):
    return read_json(status_path(folder))


class _StatusLock:
    """Cross-process lock via an exclusive lock file."""

    def __init__(self, folder):
        self.path = os.path.join(folder, ".status.lock")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.monotonic() + 5
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


def write_status(folder, payload):
    """Replace status.json. Used when a session starts."""
    body = dict(payload)
    body.setdefault("updated_at", iso_now())
    with _StatusLock(folder):
        atomic_write_json(status_path(folder), body)
    return body


def update_status(folder, updates, preserve=PRESERVE_ON_UPDATE):
    """Merge `updates` into status.json. Non-empty preserved keys stick."""
    with _StatusLock(folder):
        current = read_status(folder) or {}
        for key, value in updates.items():
            if key in preserve and current.get(key) not in (None, ""):
                continue
            current[key] = value
        current["updated_at"] = iso_now()
        atomic_write_json(status_path(folder), current)
        return current


def active_path(control_dir):
    return os.path.join(control_dir, ACTIVE_FILENAME)


def read_active(control_dir):
    return read_json(active_path(control_dir))


def write_active(control_dir, payload):
    os.makedirs(control_dir, exist_ok=True)
    atomic_write_json(active_path(control_dir), payload)


def clear_active(control_dir, pid=None):
    """Remove the lock. When pid is given, only the owning process clears it."""
    path = active_path(control_dir)
    current = read_json(path)
    if current is None:
        return
    if pid is not None and current.get("pid") != pid:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def begin_session(
    folder,
    control_dir,
    *,
    mode,
    transcript_only,
    meeting_name,
    whisper_model,
    language,
    meet_code=None,
    window_title=None,
    joined_at=None,
    break_ok=False,
    honor_existing_stop=False,
):
    """Publish status.json and the active-session lock.

    Returns True when a stop file was already waiting (automated sessions do
    not delete a fresh STOP, so a stop that won the startup race is honored).
    """
    already_stopping = os.path.isfile(session_stop_path(folder))
    if not (honor_existing_stop and already_stopping):
        clear_session_stop(folder)
        _clear_stop_request_for_folder(control_dir, folder)
        already_stopping = False
    now = iso_now()
    status = {
        "state": STATE_RECORDING,
        "pid": os.getpid(),
        "mode": mode,
        "transcript_only": bool(transcript_only),
        "meeting_name": meeting_name,
        "meet_code": meet_code,
        "window_title": window_title,
        "whisper_model": whisper_model,
        "language": language,
        "created_at": now,
        "updated_at": now,
        "recording_started_at": now,
        "recording_stopped_at": None,
        "joined_at": joined_at or now,
        "left_at": None,
        "transcription_started_at": None,
        "finished_at": None,
        "error": None,
        "delivered": False,
        "delivered_at": None,
        "folder_name": os.path.basename(os.path.abspath(folder)),
        "folder_path": os.path.abspath(folder),
    }
    write_status(folder, status)
    write_active(
        control_dir,
        {
            "pid": os.getpid(),
            "folder": os.path.abspath(folder),
            "folder_name": os.path.basename(os.path.abspath(folder)),
            "mode": mode,
            "transcript_only": bool(transcript_only),
            "started_at": now,
            "break_ok": bool(break_ok),
            "meeting_name": meeting_name,
        },
    )
    return already_stopping


def _clear_stop_request_for_folder(control_dir, folder):
    request = read_stop_request(control_dir)
    if not request:
        return
    requested = request.get("folder")
    if requested and os.path.abspath(requested) == os.path.abspath(folder):
        try:
            os.remove(stop_request_path(control_dir))
        except OSError:
            pass


def session_stop_path(folder):
    return os.path.join(folder, STOP_FILENAME)


def clear_session_stop(folder):
    path = session_stop_path(folder)
    try:
        os.remove(path)
    except OSError:
        pass


def write_session_stop(folder):
    os.makedirs(folder, exist_ok=True)
    path = session_stop_path(folder)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, b"stop\n")
    finally:
        os.close(fd)


def stop_request_path(control_dir):
    return os.path.join(control_dir, STOP_REQUEST_FILENAME)


def write_stop_request(control_dir, pid, folder):
    os.makedirs(control_dir, exist_ok=True)
    atomic_write_json(
        stop_request_path(control_dir),
        {"pid": pid, "folder": os.path.abspath(folder)},
    )


def read_stop_request(control_dir):
    return read_json(stop_request_path(control_dir))


def stop_requested(folder, control_dir, pid=None):
    """True when this session should stop.

    Honors a STOP file in the session folder, and a well-known stop request
    under the control directory when it names this pid or this folder.
    A stale request for a different session is ignored.
    """
    if os.path.isfile(session_stop_path(folder)):
        return True
    request = read_stop_request(control_dir)
    if not request:
        return False
    pid = os.getpid() if pid is None else pid
    if request.get("pid") == pid:
        return True
    requested_folder = request.get("folder")
    if requested_folder and os.path.abspath(requested_folder) == os.path.abspath(folder):
        return True
    return False


def request_stop(control_dir, folder, pid, break_ok=False):
    """Ask a running recorder to stop. Returns True if CTRL_BREAK was sent."""
    write_session_stop(folder)
    write_stop_request(control_dir, pid, folder)
    if break_ok:
        return signal_ctrl_break(pid)
    return False


def pid_alive(pid):
    """Return True if `pid` looks like a running process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query = 0x1000
    still_active = 259
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def signal_ctrl_break(pid):
    """Send CTRL_BREAK_EVENT to a Windows process group. No-op elsewhere.

    Only works when the target was started with CREATE_NEW_PROCESS_GROUP and
    still has a console. Callers also write a stop file, which is what a
    hidden pythonw recorder actually observes.
    """
    if sys.platform != "win32":
        return False
    from meeting_notes.win_signal import send_ctrl_break

    try:
        return bool(send_ctrl_break(pid))
    except Exception:
        return False


def live_session(control_dir, ignore_pid=None):
    """Return the active-session record when its pid is still running."""
    data = read_active(control_dir)
    if not data:
        return None
    pid = data.get("pid")
    if ignore_pid is not None and pid == ignore_pid:
        return None
    if not pid_alive(pid):
        return None
    return data


def outbox_dir(control_dir):
    return os.path.join(control_dir, OUTBOX_DIRNAME)


def watcher_pid_path(control_dir):
    return os.path.join(control_dir, WATCHER_PID_FILENAME)


def write_watcher_pid(control_dir, pid=None):
    os.makedirs(control_dir, exist_ok=True)
    path = watcher_pid_path(control_dir)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid() if pid is None else pid))
        handle.write("\n")


def read_watcher_pid(control_dir):
    path = watcher_pid_path(control_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def clear_watcher_pid(control_dir, pid=None):
    current = read_watcher_pid(control_dir)
    if pid is not None and current != pid:
        return
    try:
        os.remove(watcher_pid_path(control_dir))
    except OSError:
        pass
