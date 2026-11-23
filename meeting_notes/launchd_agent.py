"""Per-user LaunchAgent that keeps the macOS watcher running.

This is the macOS counterpart of the Windows scheduled task. It does not
touch Task Scheduler. The agent runs at login in the Aqua session and stays
up. On wake and on unlock it runs the same ``watch --ensure`` the Windows
task runs, including the 30s / 60s / 120s retries and the rule that a
watcher which is recording is not killed.

The plist is generated for the interpreter that installs it. No user name,
home directory, or webhook is written into the plist beyond the log paths
under the control directory the caller passes.
"""

import os
import subprocess
from xml.sax.saxutils import escape

from meeting_notes.autostart import AutostartError, autostart_meta_path, find_pythonw, read_autostart
from meeting_notes.macos_capture import find_capture_helper, helper_wait_argv


LABEL = "com.meeting-notes.watch"
PLIST_FILENAME = LABEL + ".plist"


def launch_agents_dir():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")


def plist_path(agents_dir=None):
    return os.path.join(agents_dir or launch_agents_dir(), PLIST_FILENAME)


def render_plist(python, control_dir, label=LABEL):
    """UTF-8 launchd plist. `python` is the interpreter that runs supervise."""
    out_log = escape(os.path.join(control_dir, "launchd.log"))
    err_log = escape(os.path.join(control_dir, "launchd.err.log"))
    label = escape(label)
    path_env = escape(
        "/opt/homebrew/bin:/usr/local/bin:%s:/usr/bin:/bin:/usr/sbin:/sbin"
        % os.path.join(control_dir, "bin")
    )
    args = "\n".join(
        "    <string>%s</string>" % escape(part)
        for part in (os.path.abspath(python), "-m", "meeting_notes.cli", "autostart", "supervise")
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        "  <string>%s</string>\n" % label
        + "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        "%s\n" % args
        + "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <dict>\n"
        "    <key>SuccessfulExit</key>\n"
        "    <false/>\n"
        "  </dict>\n"
        "  <key>LimitLoadToSessionType</key>\n"
        "  <string>Aqua</string>\n"
        "  <key>ProcessType</key>\n"
        "  <string>Background</string>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        "    <key>PATH</key>\n"
        "    <string>%s</string>\n" % path_env
        + "  </dict>\n"
        "  <key>StandardOutPath</key>\n"
        "  <string>%s</string>\n" % out_log
        + "  <key>StandardErrorPath</key>\n"
        "  <string>%s</string>\n" % err_log
        + "</dict>\n"
        "</plist>\n"
    )


def _write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".partial"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _run_error(exc):
    detail = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
    return " ".join(str(detail).split()) or exc.__class__.__name__


def install_launch_agent(
    control_dir,
    python=None,
    agents_dir=None,
    run=None,
    uid=None,
):
    """Write the plist and bootstrap it into the current GUI session.

    Raises AutostartError when ``launchctl bootstrap`` fails. A failed
    install leaves the plist on disk so the error can be retried, and does
    not write a Windows scheduled task.
    """
    from meeting_notes.atomic_io import atomic_write_json

    python = python or find_pythonw()
    agents_dir = agents_dir if agents_dir is not None else launch_agents_dir()
    run = run or subprocess.run
    if uid is None:
        uid = os.getuid()
    os.makedirs(control_dir, exist_ok=True)
    path = plist_path(agents_dir)
    _write_text(path, render_plist(python, control_dir))
    domain = "gui/%s" % uid
    target = "%s/%s" % (domain, LABEL)
    run(["launchctl", "bootout", target], check=False, capture_output=True, text=True)
    try:
        run(
            ["launchctl", "bootstrap", domain, path],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        raise AutostartError(
            "Could not install the LaunchAgent %s. launchctl said: %s"
            % (LABEL, _run_error(exc))
        ) from exc
    run(["launchctl", "enable", target], check=False, capture_output=True, text=True)
    run(["launchctl", "kickstart", "-k", target], check=False, capture_output=True, text=True)
    meta = {
        "method": "launchd",
        "label": LABEL,
        "plist": path,
        "python": os.path.abspath(python),
        "command": '"%s" -m meeting_notes.cli autostart supervise' % os.path.abspath(python),
        "triggers": ["login", "wake", "unlock"],
        "retry_seconds": [30, 60, 120],
    }
    atomic_write_json(autostart_meta_path(control_dir), meta)
    return meta


def uninstall_launch_agent(control_dir, agents_dir=None, run=None, uid=None):
    """Unload the agent and delete the plist. Returns the paths removed."""
    run = run or subprocess.run
    if uid is None:
        uid = os.getuid()
    agents_dir = agents_dir if agents_dir is not None else launch_agents_dir()
    meta = read_autostart(control_dir) or {}
    removed = []
    target = "gui/%s/%s" % (uid, meta.get("label") or LABEL)
    run(["launchctl", "bootout", target], check=False, capture_output=True, text=True)
    for path in (meta.get("plist"), plist_path(agents_dir)):
        if path and os.path.isfile(path) and path not in removed:
            try:
                os.remove(path)
                removed.append(path)
            except OSError:
                pass
    if meta.get("method") == "launchd":
        meta_path = autostart_meta_path(control_dir)
        if os.path.isfile(meta_path):
            try:
                os.remove(meta_path)
                removed.append(meta_path)
            except OSError:
                pass
    return removed


def wait_for_session_event(helper=None, run=None, sleep=None):
    """Block until the Mac wakes or the session unlocks.

    The helper exits after one event. A missing helper waits and returns
    ``helper-missing`` so the supervisor retries instead of spinning.
    """
    import time

    sleep = sleep or time.sleep
    run = run or subprocess.run
    if helper is None:
        helper = find_capture_helper()
    if not helper:
        sleep(60)
        return "helper-missing"
    try:
        completed = run(
            helper_wait_argv(helper),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        sleep(30)
        return "wait-failed"
    if getattr(completed, "returncode", 0) not in (0, None):
        sleep(30)
        return "wait-failed"
    text = (getattr(completed, "stdout", "") or "").strip().splitlines()
    if text and text[-1] in ("wake", "unlock"):
        return text[-1]
    return "wake-or-unlock"


def _log_ensure(log, reason, result):
    message = getattr(result, "user_message", None) or str(result)
    log.info("launchd ensure (%s): %s", reason, message)


WATCHDOG_INTERVAL_SECONDS = 30.0


def watcher_needs_start(control_dir, now=None):
    """True when no watcher is running. Quiet: writes no log and kills nothing."""
    import time

    from meeting_notes.session import pid_alive
    from meeting_notes.watch_guard import (
        decide,
        lock_is_held,
        read_heartbeat,
        read_instance,
        read_watcher_recording,
    )

    current = time.time() if now is None else float(now)
    decision = decide(
        read_instance(control_dir),
        read_heartbeat(control_dir),
        current,
        pid_alive,
        lock_is_held(control_dir),
        active_session=read_watcher_recording(control_dir, is_alive=pid_alive),
    )
    return decision.action == "start"


def start_watchdog(control_dir, ensure, log, interval=WATCHDOG_INTERVAL_SECONDS, needs_start=None, sleep=None):
    """Restart a dead watcher within ~``interval`` s, with no wake or unlock event.

    Returns a function that stops the watchdog thread.
    """
    import threading
    import time

    needs = needs_start or (lambda: watcher_needs_start(control_dir))
    stop = threading.Event()
    nap = sleep or stop.wait

    def _loop():
        while not stop.is_set():
            nap(interval)
            if stop.is_set():
                break
            try:
                if needs():
                    log.warning("watchdog: no watcher is running; starting one")
                    _log_ensure(log, "watchdog", ensure("launchd watchdog"))
            except Exception:
                log.exception("watchdog check failed")

    thread = threading.Thread(target=_loop, name="watcher-watchdog", daemon=True)
    thread.start()
    return stop.set


def supervise(control_dir, ensure=None, wait_for_session=None, log=None, max_events=None, watchdog_interval=WATCHDOG_INTERVAL_SECONDS):
    """Run ensure at login, then again on every wake and unlock.

    ``ensure`` is ``watch --ensure``. That is what restarts a crashed or
    frozen watcher and what refuses to kill one that is mid-recording.
    This function does not signal the watcher itself.
    """
    if log is None:
        from meeting_notes.watcher import configure_watch_logging

        log, _path = configure_watch_logging(control_dir)
    if ensure is None:
        from meeting_notes.watch_guard import ensure_until_running

        def ensure(_reason):
            return ensure_until_running(control_dir)

    log.info("launchd supervisor started: login")
    _log_ensure(log, "login", ensure("launchd login"))
    stop_watchdog = start_watchdog(control_dir, ensure, log, interval=watchdog_interval)
    seen = 0
    while max_events is None or seen < max_events:
        event = (wait_for_session or wait_for_session_event)()
        seen += 1
        if not event:
            continue
        log.info("launchd session event: %s", event)
        _log_ensure(log, event, ensure("launchd %s" % event))
    stop_watchdog()
    return seen
