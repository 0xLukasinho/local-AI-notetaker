"""Single-instance lock, heartbeat, and self-heal for the Meet watcher.

A running watcher holds an exclusive lock for its whole life and rewrites
``heartbeat.json`` about every 30 seconds. ``meeting-notes watch --ensure``
reads that file:

- a live watcher with a fresh heartbeat is left alone
- a dead process, or a lock left behind by a crash, is replaced
- a heartbeat older than three minutes is treated as a hang: the process is
  killed and a new watcher is started
- a heartbeat whose state is ``recording`` is not killed unless it is older
  than ten minutes, so a long transcription is not cut off

The pid in the lock is killed only when that process still holds the lock.
A pid that was reused by an unrelated program is not killed.

On Windows the replacement process is created with WMI so it is not a child
of the scheduled task. Task Scheduler would otherwise stop it when ``--ensure``
exits.
"""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from meeting_notes.atomic_io import atomic_write_json
from meeting_notes.autostart import find_pythonw
from meeting_notes.session import iso_now

HEARTBEAT_FILENAME = "heartbeat.json"
INSTANCE_FILENAME = "watcher.json"
LOCK_FILENAME = "watcher.lock"
START_REASON_FILENAME = "watch-start-reason.json"

HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_STALE_SECONDS = 180
HEARTBEAT_RECORDING_DEAD_SECONDS = 600
HEALTH_PING_DOWN_SECONDS = 15 * 60
START_REASON_MAX_AGE_SECONDS = 120
START_REASON_ENV = "MEETING_NOTES_WATCH_START_REASON"

_MUTEX_NAME = "Local\\MeetingNotesWatch"
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x00000080


def package_version():
    try:
        from importlib.metadata import version

        return version("meeting-notes")
    except Exception:
        return "0.1.0"


def heartbeat_path(control_dir):
    return os.path.join(control_dir, HEARTBEAT_FILENAME)


def instance_path(control_dir):
    return os.path.join(control_dir, INSTANCE_FILENAME)


def lock_path(control_dir):
    return os.path.join(control_dir, LOCK_FILENAME)


def start_reason_path(control_dir):
    return os.path.join(control_dir, START_REASON_FILENAME)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_heartbeat(control_dir):
    return _read_json(heartbeat_path(control_dir))


def read_instance(control_dir):
    return _read_json(instance_path(control_dir))


def write_instance(control_dir, pid, version, started_at_unix):
    os.makedirs(control_dir, exist_ok=True)
    payload = {
        "pid": int(pid),
        "started_at": iso_now(),
        "started_at_unix": float(started_at_unix),
        "version": version,
    }
    atomic_write_json(instance_path(control_dir), payload)
    return payload


def clear_instance(control_dir, pid):
    current = read_instance(control_dir)
    if current and current.get("pid") not in (None, pid):
        return
    try:
        os.remove(instance_path(control_dir))
    except OSError:
        pass


class Heartbeat:
    """Rewrite the heartbeat file, at most once per interval unless forced."""

    def __init__(
        self,
        control_dir,
        interval=HEARTBEAT_INTERVAL_SECONDS,
        now=None,
        version=None,
        pid=None,
        iso=None,
    ):
        self.control_dir = control_dir
        self.interval = float(interval)
        self.now = now or time.time
        self.version = version or package_version()
        self.pid = os.getpid() if pid is None else int(pid)
        self.iso = iso or (lambda _unix: iso_now())
        self._last = None

    def beat(self, force=False, state="idle"):
        current = float(self.now())
        if (
            not force
            and self._last is not None
            and current - self._last < self.interval
        ):
            return False
        if state not in ("idle", "recording"):
            state = "idle"
        payload = {
            "timestamp": self.iso(current),
            "unix": current,
            "pid": self.pid,
            "version": self.version,
            "state": state,
        }
        atomic_write_json(heartbeat_path(self.control_dir), payload)
        self._last = current
        return True


class InstanceLock:
    def __init__(self, handle, mutex=None, mutex_api=None):
        self._handle = handle
        self._mutex = mutex
        self._mutex_api = mutex_api
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        _unlock_file(self._handle)
        try:
            self._handle.close()
        except OSError:
            pass
        if self._mutex is not None and self._mutex_api is not None:
            try:
                self._mutex_api.ReleaseMutex(self._mutex)
            except Exception:
                pass
            try:
                self._mutex_api.CloseHandle(self._mutex)
            except Exception:
                pass


def acquire_instance_lock(control_dir):
    """Take the watcher lock, or return None if another live process holds it.

    The lock is released when the process exits, including after a crash, so
    the next acquire does not get stuck on a stale file.
    """
    os.makedirs(control_dir, exist_ok=True)
    mutex = None
    mutex_api = None
    if sys.platform == "win32":
        mutex, mutex_api = _try_acquire_mutex()
        if mutex is None:
            return None
    try:
        handle = _try_lock_file(lock_path(control_dir))
    except Exception:
        if mutex is not None and mutex_api is not None:
            mutex_api.ReleaseMutex(mutex)
            mutex_api.CloseHandle(mutex)
        raise
    if handle is None:
        if mutex is not None and mutex_api is not None:
            mutex_api.ReleaseMutex(mutex)
            mutex_api.CloseHandle(mutex)
        return None
    return InstanceLock(handle, mutex, mutex_api)


def lock_is_held(control_dir):
    """True when some process currently holds the watcher lock."""
    held = acquire_instance_lock(control_dir)
    if held is None:
        return True
    held.release()
    return False


def _try_lock_file(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    # Binary mode so the Windows byte-range lock lines up with the first byte.
    handle = os.fdopen(fd, "rb+")
    try:
        _ensure_lock_byte(handle)
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return None
    except Exception:
        handle.close()
        raise
    return handle


def _ensure_lock_byte(handle):
    handle.seek(0, os.SEEK_END)
    if handle.tell() < 1:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def _unlock_file(handle):
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _try_acquire_mutex():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    result = kernel32.WaitForSingleObject(handle, 0)
    if result in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        return handle, kernel32
    kernel32.CloseHandle(handle)
    return None, kernel32


@dataclass
class Decision:
    action: str
    reason: str
    pid: int = None
    kill: bool = False
    state: str = None
    heartbeat_age: float = None
    user_message: str = ""

    @property
    def ok(self):
        return self.action in ("healthy", "spare", "start", "restart")


def _as_pid(value):
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return pid


def _heartbeat_age(heartbeat, now):
    if not heartbeat or not isinstance(heartbeat.get("unix"), (int, float)):
        return None
    age = float(now) - float(heartbeat["unix"])
    if age < 0:
        return 0.0
    return age


def decide(instance, heartbeat, now, is_alive, lock_held):
    """Choose healthy, spare, start, restart, or failed. Pure: no I/O."""
    instance_pid = _as_pid(instance.get("pid")) if instance else None
    heartbeat_pid = _as_pid(heartbeat.get("pid")) if heartbeat else None
    instance_alive = bool(instance_pid) and bool(is_alive(instance_pid))
    heartbeat_alive = bool(heartbeat_pid) and bool(is_alive(heartbeat_pid))
    pid = instance_pid if instance_alive else (heartbeat_pid if heartbeat_alive else None)

    if not lock_held:
        if instance_alive:
            return Decision(
                "start",
                f"recorded pid {instance_pid} does not hold the watcher lock; not killing it",
                pid=instance_pid,
                kill=False,
            )
        if instance_pid:
            return Decision(
                "start",
                f"stale lock for pid {instance_pid} (process is not running)",
                kill=False,
            )
        return Decision("start", "no watcher is running", kill=False)

    if pid is None:
        return Decision(
            "failed",
            "watcher lock is held but no living pid is recorded; not starting a second watcher",
            kill=False,
        )

    matches = (
        heartbeat is not None
        and heartbeat_pid == pid
        and isinstance(heartbeat.get("unix"), (int, float))
    )
    if not matches:
        started = None
        if instance and instance_pid == pid:
            started = instance.get("started_at_unix")
        if isinstance(started, (int, float)) and float(now) - float(started) < HEARTBEAT_STALE_SECONDS:
            return Decision(
                "healthy",
                f"watcher pid {pid} started recently and has no heartbeat yet",
                pid=pid,
            )
        return Decision(
            "restart",
            f"watcher pid {pid} has no heartbeat",
            pid=pid,
            kill=True,
        )

    age = _heartbeat_age(heartbeat, now)
    state = heartbeat.get("state") or "idle"
    if age < HEARTBEAT_STALE_SECONDS:
        return Decision(
            "healthy",
            f"watcher pid {pid} is healthy",
            pid=pid,
            state=state,
            heartbeat_age=age,
        )
    if state == "recording" and age <= HEARTBEAT_RECORDING_DEAD_SECONDS:
        return Decision(
            "spare",
            f"watcher pid {pid} heartbeat is stale but state is recording",
            pid=pid,
            kill=False,
            state=state,
            heartbeat_age=age,
        )
    why = "heartbeat is stale"
    if state == "recording":
        why = "heartbeat is stale while recording and past the dead limit"
    return Decision(
        "restart",
        f"watcher pid {pid} {why}",
        pid=pid,
        kill=True,
        state=state,
        heartbeat_age=age,
    )


def _age_text(age):
    if age is None:
        return "unknown"
    return str(int(age))


def describe_heartbeat(control_dir, now=None):
    """One status line for ``watch --status``."""
    now = time.time() if now is None else float(now)
    heartbeat = read_heartbeat(control_dir)
    if not heartbeat:
        return "Heartbeat: missing"
    age = _heartbeat_age(heartbeat, now)
    state = heartbeat.get("state") or "unknown"
    stamp = heartbeat.get("timestamp") or "unknown time"
    version = heartbeat.get("version") or "unknown"
    pid = heartbeat.get("pid")
    note = ""
    if age is not None and age >= HEARTBEAT_STALE_SECONDS:
        if state == "recording" and age <= HEARTBEAT_RECORDING_DEAD_SECONDS:
            note = "; recording is left alone until the heartbeat is older than 10 minutes"
        else:
            note = "; stale"
    age_text = "unknown" if age is None else f"{int(age)}s"
    return (
        f"Heartbeat: {stamp} (age {age_text}, state={state}, "
        f"pid={pid}, version={version}){note}"
    )


def ensure_watcher(
    control_dir,
    log=None,
    now=None,
    is_alive=None,
    lock_held_fn=None,
    kill=None,
    spawn=None,
    wait_until_dead=None,
    sleep=None,
):
    """Start a detached watcher when the current one is missing or hung.

    Returns a Decision. A healthy watcher makes this return immediately.
    """
    from meeting_notes.session import pid_alive

    if log is None:
        from meeting_notes.watcher import configure_watch_logging

        log, _path = configure_watch_logging(control_dir)
    clock = now or time.time
    current = float(clock() if callable(clock) else clock)
    alive = is_alive or pid_alive
    held = lock_is_held(control_dir) if lock_held_fn is None else bool(lock_held_fn())
    decision = decide(
        read_instance(control_dir),
        read_heartbeat(control_dir),
        current,
        alive,
        held,
    )
    if decision.action == "healthy":
        if decision.heartbeat_age is None:
            age_text = "no heartbeat yet"
        else:
            age_text = f"heartbeat age {_age_text(decision.heartbeat_age)}s"
        decision.user_message = (
            "ensure: watcher already running "
            f"(pid {decision.pid}, state {decision.state or 'unknown'}, {age_text})"
        )
        log.info(decision.user_message)
        return decision
    if decision.action == "spare":
        decision.user_message = (
            "ensure: not killing a recording watcher "
            f"(pid {decision.pid}, heartbeat age {_age_text(decision.heartbeat_age)}s, "
            f"dead limit {int(HEARTBEAT_RECORDING_DEAD_SECONDS)}s)"
        )
        log.warning(decision.user_message)
        return decision
    if decision.action == "failed":
        decision.user_message = f"ensure: {decision.reason}"
        log.error(decision.user_message)
        return decision

    reason = decision.reason
    if decision.kill:
        hang = (
            f"hang-kill: pid={decision.pid} state={decision.state or 'unknown'} "
            f"heartbeat_age={_age_text(decision.heartbeat_age)}s reason={decision.reason}"
        )
        log.warning(hang)
        killer = kill or terminate_watcher
        try:
            killer(decision.pid)
        except Exception as exc:
            decision.action = "failed"
            decision.user_message = f"ensure: hang-kill failed: {exc}"
            log.error(decision.user_message)
            return decision
        waiter = wait_until_dead or (
            lambda pid: _wait_until_dead(pid, alive, sleep=sleep or time.sleep)
        )
        if not waiter(decision.pid):
            decision.action = "failed"
            decision.user_message = (
                f"ensure: hang-kill failed; pid {decision.pid} still alive; "
                "not starting a second watcher"
            )
            log.error(decision.user_message)
            return decision
        decision.user_message = f"ensure: restarting watcher ({decision.reason})"
        log.warning(decision.user_message)
        reason = f"restart after hang-kill: {decision.reason}"
    else:
        decision.user_message = f"ensure: starting watcher ({decision.reason})"
        log.warning(decision.user_message)
        reason = f"ensure start: {decision.reason}"

    try:
        method = spawn_watcher(control_dir, reason, spawn=spawn)
    except Exception as exc:
        decision.action = "failed"
        decision.user_message = f"ensure: failed to start watcher: {exc}"
        log.error(decision.user_message)
        return decision
    log.info("ensure: watcher spawn requested (%s)", method)
    if decision.kill:
        decision.action = "restart"
    else:
        decision.action = "start"
    return decision


def _wait_until_dead(pid, is_alive, timeout=5.0, sleep=None):
    sleep = sleep or time.sleep
    deadline = time.monotonic() + float(timeout)
    while is_alive(pid):
        if time.monotonic() >= deadline:
            return False
        sleep(0.1)
    return True


def terminate_watcher(pid):
    """Stop a hung watcher. Refuses pid 0, negative pids, and this process."""
    pid = int(pid)
    if pid <= 0 or pid == os.getpid():
        raise RuntimeError(f"refusing to kill pid {pid}")
    if sys.platform == "win32":
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or f"taskkill exited {completed.returncode}")
        return
    import signal

    # SIGKILL, not SIGTERM: a hung watcher may not be running Python bytecode.
    # Kill only this pid. The Windows path uses taskkill /T so a stuck recorder
    # exits with the watcher; this branch is the non-Windows fallback.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def spawn_watcher(control_dir, reason, spawn=None):
    """Start ``meeting-notes watch`` detached. Tests pass ``spawn``."""
    write_start_reason(control_dir, reason)
    if spawn is not None:
        spawn(reason)
        return "injected"
    argv = [find_pythonw(), "-m", "meeting_notes.cli", "watch"]
    if sys.platform == "win32":
        try:
            _spawn_wmi(argv)
            return "wmi"
        except Exception as first:
            try:
                _spawn_popen(argv, reason)
            except Exception as second:
                raise RuntimeError(
                    f"wmi failed ({first}); detached start failed ({second})"
                ) from second
            return f"popen-fallback ({first})"
    _spawn_popen(argv, reason)
    return "posix"


def write_start_reason(control_dir, reason):
    os.makedirs(control_dir, exist_ok=True)
    text = " ".join(str(reason or "").split())
    atomic_write_json(
        start_reason_path(control_dir),
        {"reason": text[:500], "unix": time.time()},
    )


def consume_start_reason(control_dir, environ=None, now=None):
    """Read the reason recorded by --ensure, once, after this process holds the lock."""
    environ = os.environ if environ is None else environ
    now = time.time() if now is None else float(now)
    env_reason = " ".join(str(environ.get(START_REASON_ENV) or "").split())
    path = start_reason_path(control_dir)
    data = _read_json(path)
    try:
        os.remove(path)
    except OSError:
        pass
    if env_reason:
        return env_reason[:500]
    if not data:
        return "manual start"
    reason = " ".join(str(data.get("reason") or "").split())
    unix = data.get("unix")
    if not reason or not isinstance(unix, (int, float)):
        return "manual start"
    if now - float(unix) > START_REASON_MAX_AGE_SECONDS:
        return "manual start"
    return reason[:500]


def _spawn_popen(argv, reason):
    env = os.environ.copy()
    env[START_REASON_ENV] = " ".join(str(reason or "").split())[:500]
    kwargs = {
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        detached = 0x00000008
        create_no_window = 0x08000000
        new_group = 0x00000200
        kwargs["creationflags"] = detached | create_no_window | new_group
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def _spawn_wmi(argv):
    """Create the watcher from WMI so Task Scheduler does not kill it later."""
    command_line = subprocess.list2cmdline(argv)
    quoted = "'" + command_line.replace("'", "''") + "'"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$create = @{{ CommandLine = {quoted} }}; "
        "$result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments $create; "
        "if ($result.ReturnValue -ne 0) { "
        "Write-Error ('Win32_Process.Create failed: ' + $result.ReturnValue); exit 1 }"
    )
    create_no_window = 0x08000000
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=create_no_window,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"powershell exited {completed.returncode}")


def build_health_payload(down_seconds, previous, pid, version, started_at):
    """A watcher-health event. It is not a transcript and has no webhook secret."""
    previous = previous or {}
    return {
        "type": "watcher-health",
        "down_seconds": int(down_seconds),
        "previous_state": previous.get("state"),
        "previous_heartbeat_at": previous.get("timestamp"),
        "pid": pid,
        "version": version,
        "started_at": started_at,
    }


def maybe_send_health(
    config,
    previous,
    now,
    log=None,
    poster=None,
    sleep=None,
    same_local_day=None,
    pid=None,
    version=None,
    started_at=None,
):
    """POST ``type: watcher-health`` when today's gap is longer than 15 minutes.

    Off unless ``watcher_health_ping`` is true. A missing webhook skips the
    send. Nothing is written to the transcript outbox. Returns True when a
    ping was sent.
    """
    if not bool(getattr(config, "watcher_health_ping", False)):
        return False
    if not getattr(config, "webhook_url", ""):
        if log is not None:
            log.info("watcher-health ping skipped: webhook_url is empty")
        return False
    if not previous or not isinstance(previous.get("unix"), (int, float)):
        return False
    down = float(now) - float(previous["unix"])
    if down < HEALTH_PING_DOWN_SECONDS:
        return False
    same_day = same_local_day or _same_local_day
    if not same_day(float(previous["unix"]), float(now)):
        if log is not None:
            log.info(
                "watcher-health ping skipped: downtime crossed local midnight (%.0fs)",
                down,
            )
        return False
    from meeting_notes.deliver import post_with_retry, redact_secret, urllib_poster

    payload = build_health_payload(
        down,
        previous,
        os.getpid() if pid is None else pid,
        version or package_version(),
        started_at or iso_now(),
    )
    poster = poster or urllib_poster
    sleep = sleep or time.sleep
    ok, error = post_with_retry(
        config,
        payload,
        poster,
        sleep,
        attempts=2,
        backoff=1,
    )
    if log is None:
        return bool(ok)
    if ok:
        log.info("Sent watcher-health ping (down_seconds=%s)", payload["down_seconds"])
    else:
        secret = getattr(config, "webhook_auth_value", "")
        log.error(
            "watcher-health ping failed: %s",
            redact_secret(error or "delivery failed", secret),
        )
    return bool(ok)


def _same_local_day(previous_unix, now_unix):
    from datetime import datetime

    previous = datetime.fromtimestamp(previous_unix).astimezone().date()
    current = datetime.fromtimestamp(now_unix).astimezone().date()
    return previous == current
