"""Long-running call watcher (Google Meet and Slack Huddle).

The loop is platform-neutral: detectors, the session launcher, and delivery
are injected. On Windows, `serve` wires the real window and mic probes and a
hidden recorder subprocess. The mic probe ORs the consent-store registry with
WASAPI capture sessions. One session records at a time. A call that is already
in progress keeps going if the other source appears; a manual session blocks
a watcher start for the rest of that call.
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
from dataclasses import dataclass

from meeting_notes.autostart import find_pythonw
from meeting_notes.watch_guard import (
    HEARTBEAT_INTERVAL_SECONDS,
    Heartbeat,
    acquire_instance_lock,
    clear_instance,
    consume_start_reason,
    maybe_send_health,
    package_version,
    read_heartbeat,
    read_instance,
    read_watcher_recording,
    write_instance,
)
from meeting_notes.deliver import deliver_folder, retry_outbox
from meeting_notes.device_log import DEVICE_LOG_FILENAME, read_new_device_events
from meeting_notes.meet_detect import session_name
from meeting_notes.session import (
    allocate_meeting_dir,
    clear_watcher_pid,
    iso_now,
    live_session,
    outbox_dir,
    pid_alive,
    read_status,
    request_stop,
    update_status,
    write_watcher_pid,
    LOG_FILENAME,
)


CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

_RUNNING = {"recording", "transcribing", "running"}
_FINISHED_OK = {"done"}
_FINISHED_BAD = {"failed", "exited", "timeout"}


@dataclass
class SessionSpec:
    name: str
    folder: str
    meet_code: str
    window_title: str
    joined_at: str
    transcript_only: bool
    whisper_model: str
    language: str
    source: str = ""


@dataclass
class RunningSession:
    folder: str
    pid: int
    popen: object = None
    log_handle: object = None
    break_ok: bool = False


def build_start_argv(spec, executable=None):
    """Argv for a hidden `meeting-notes start` of one watcher session."""
    program = executable or find_pythonw()
    argv = [
        program,
        "-m",
        "meeting_notes.cli",
        "start",
        spec.name,
        "--output-dir",
        spec.folder,
        "--automated",
        "--whisper-model",
        spec.whisper_model,
        "--language",
        spec.language,
        "--joined-at",
        spec.joined_at,
    ]
    if spec.transcript_only:
        argv.append("--transcript-only")
    if spec.meet_code:
        argv.extend(["--meet-code", spec.meet_code])
    if spec.window_title:
        argv.extend(["--window-title", spec.window_title])
    if getattr(spec, "source", ""):
        argv.extend(["--source", spec.source])
    return argv


def hidden_popen_kwargs():
    """Creation flags for a recorder with no console, in its own process group.

    CREATE_NEW_PROCESS_GROUP lets `meeting-notes stop` target CTRL_BREAK_EVENT
    at this pid. CREATE_NO_WINDOW keeps python.exe from flashing a console.
    pythonw has no console either way; the stop file is what actually ends a
    hidden process. CTRL_BREAK is a best-effort extra.
    """
    if sys.platform != "win32":
        return {}
    return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}


class FileLockStore:
    def __init__(self, control_dir):
        self.control_dir = control_dir

    def current(self):
        return live_session(self.control_dir)


class SubprocessLauncher:
    """Start `meeting-notes start` and stop it via the session STOP file."""

    def __init__(self, control_dir, popen=None):
        self.control_dir = control_dir
        self._popen = popen or subprocess.Popen

    def start(self, spec):
        os.makedirs(spec.folder, exist_ok=True)
        log_path = os.path.join(spec.folder, "recorder.log")
        log_handle = open(log_path, "a", encoding="utf-8")
        argv = build_start_argv(spec)
        kwargs = hidden_popen_kwargs()
        try:
            proc = self._popen(
                argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **kwargs,
            )
        except Exception:
            log_handle.close()
            raise
        return RunningSession(
            folder=spec.folder,
            pid=proc.pid,
            popen=proc,
            log_handle=log_handle,
            break_ok=sys.platform == "win32",
        )

    def request_stop(self, session):
        update_status(session.folder, {"left_at": iso_now()})
        request_stop(
            self.control_dir,
            session.folder,
            session.pid,
            break_ok=session.break_ok,
        )

    def state(self, session):
        status = read_status(session.folder) or {}
        file_state = status.get("state")
        proc = session.popen
        if proc is not None:
            code = proc.poll()
            if code is None:
                return file_state or "recording"
            if file_state in ("done", "failed"):
                self._close_log(session)
                return file_state
            self._close_log(session)
            if code == 0 and file_state:
                return file_state
            return "failed" if code != 0 else (file_state or "done")
        if file_state:
            return file_state
        if session.pid and pid_alive(session.pid):
            return "recording"
        return "exited"

    def _close_log(self, session):
        handle = session.log_handle
        session.log_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


class MeetWatcher:
    def __init__(
        self,
        config,
        detector,
        launcher,
        deliver_fn,
        lock_store,
        outbox_retry,
        sleep,
        monotonic,
        wall_iso,
        wall_datetime,
        output_dir,
        log,
        heartbeat=None,
    ):
        self.config = config
        self.detector = detector
        self.launcher = launcher
        self.deliver_fn = deliver_fn
        self.lock_store = lock_store
        self.outbox_retry = outbox_retry
        self.sleep = sleep
        self.monotonic = monotonic
        self.wall_iso = wall_iso
        self.wall_datetime = wall_datetime
        self.output_dir = output_dir
        self.log = log
        self.heartbeat = heartbeat if heartbeat is not None else None
        self.activity = "idle"
        self._last_beat_state = None
        self._last_session_folder = None
        self._last_session_pid = None
        from meeting_notes.meet_detect import CallDebouncer

        self.debouncer = CallDebouncer(
            config.join_debounce_seconds,
            config.leave_debounce_seconds,
        )
        self.session = None
        self.suppressed = False
        self.delivered = False
        self._last_outbox = None
        self._last_seen_key = None
        self._device_log_offsets = {}

    def run(self, stop_event=None, max_ticks=None):
        self._retry_outbox()
        self._last_outbox = self.monotonic()
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            if stop_event is not None and stop_event.is_set():
                self.shutdown()
                return
            self.tick()
            ticks += 1
            self.sleep(self.config.poll_interval_seconds)

    def tick(self):
        now = self.monotonic()
        if (
            self._last_outbox is not None
            and now - self._last_outbox >= self.config.outbox_retry_seconds
        ):
            self._retry_outbox()
            self._last_outbox = now
        # arming: after the first coincident sample, either signal may finish
        # the join hold (the Meet tab does not have to stay focused).
        # active/dropping: a mute or a background tab must not look like a hangup.
        in_call = self.debouncer.phase in ("arming", "active", "dropping")
        sighting = self.detector.poll(in_call=in_call)
        self._log_signal(sighting)
        events = self.debouncer.update(sighting, now)
        for event in events:
            if event == "join":
                self._on_join()
            elif event == "leave":
                self._on_leave()
        self._poll_child()
        if self.session is not None:
            self._mirror_device_log(self.session.folder)
        self._beat()

    def _active_watch_recording(self):
        """Live watcher session from the lock, or None.

        The recorder is a child process. Its ``active.json`` (mode ``watch``)
        is what makes the heartbeat say recording when this process's own
        activity flag is still idle.
        """
        current_fn = getattr(self.lock_store, "current", None)
        if current_fn is None:
            return None
        try:
            current = current_fn()
        except Exception:
            return None
        if not isinstance(current, dict) or current.get("mode") != "watch":
            return None
        return current

    def _recording_marker(self):
        active = self._active_watch_recording()
        if active is not None:
            return active.get("folder") or active.get("folder_name"), active.get("pid")
        if self.activity == "recording" and self.session is not None:
            return self.session.folder, self.session.pid
        return None, None

    def health_state(self):
        folder, pid = self._recording_marker()
        if folder or pid or self.activity == "recording":
            return "recording"
        return "idle"

    def _beat(self):
        heartbeat = self.heartbeat
        if heartbeat is None:
            return
        state = self.health_state()
        folder, pid = (None, None)
        if state == "recording":
            folder, pid = self._recording_marker()
        force = (
            state != self._last_beat_state
            or folder != self._last_session_folder
            or pid != self._last_session_pid
        )
        self._last_beat_state = state
        self._last_session_folder = folder
        self._last_session_pid = pid
        try:
            heartbeat.beat(
                force=force,
                state=state,
                session_folder=folder,
                session_pid=pid,
            )
        except Exception as exc:
            self.log.error("Could not write the heartbeat: %s", exc)

    def shutdown(self):
        """Stop a recording we started and deliver it before the process exits."""
        if self.session is not None and not self.delivered:
            self._on_leave()
        else:
            self.session = None

    def _log_signal(self, sighting):
        key = sighting.key if sighting is not None else None
        if key == self._last_seen_key:
            return
        if key:
            self.log.info("Call signal present: %s", sighting.window_title)
        elif self._last_seen_key:
            self.log.info("Call signal lost")
        self._last_seen_key = key

    def _on_join(self):
        active = None
        try:
            active = self.lock_store.current()
        except Exception as exc:
            self.log.error("Could not read the session lock: %s", exc)
        if active is not None:
            self.suppressed = True
            self.session = None
            pending = getattr(self.debouncer.active, "source", "") or "call"
            self.log.warning(
                "Not starting a %s recording: a session is already running "
                "(pid %s, mode %s, folder %s).",
                pending,
                active.get("pid"),
                active.get("mode"),
                active.get("folder"),
            )
            return
        sighting = self.debouncer.active
        if sighting is None:
            return
        when = self.wall_datetime()
        name = session_name(sighting, when=when)
        folder = allocate_meeting_dir(
            self.output_dir,
            name,
            unique=True,
            when=when,
        )
        spec = SessionSpec(
            name=name,
            folder=folder,
            meet_code=sighting.meet_code,
            window_title=sighting.window_title,
            joined_at=self.wall_iso(),
            transcript_only=bool(self.config.watcher_transcript_only),
            whisper_model=self.config.whisper_model,
            language=self.config.language,
            source=getattr(sighting, "source", "") or "",
        )
        try:
            self.session = self.launcher.start(spec)
        except Exception as exc:
            self.suppressed = True
            self.session = None
            self.log.error("Failed to start session: %s", exc)
            return
        self.delivered = False
        self.suppressed = False
        self.activity = "recording"
        self.log.info(
            "Started transcript session %s (%s, source %s)",
            folder,
            name,
            spec.source or "unspecified",
        )
        self._beat()

    def _on_leave(self):
        session = self.session
        suppressed = self.suppressed
        self.session = None
        self.suppressed = False
        if suppressed or session is None:
            self.activity = "idle"
            self.log.info("Call ended with no watcher recording to stop.")
            self._beat()
            return
        if self.delivered:
            self.activity = "idle"
            self.log.info("Call ended; session %s was already delivered.", session.folder)
            self._beat()
            return
        self.activity = "recording"
        self.log.info("Call ended; stopping %s", session.folder)
        try:
            self._mirror_device_log(session.folder)
            try:
                self.launcher.request_stop(session)
            except Exception as exc:
                self.log.error("Failed to signal stop for %s: %s", session.folder, exc)
            state = self._wait_until_finished(session)
            self._mirror_device_log(session.folder)
            if state in _FINISHED_OK:
                self._deliver(session.folder)
            else:
                self.log.error(
                    "Session %s finished with state %s; not delivering.",
                    session.folder,
                    state,
                )
            self.delivered = True
        finally:
            self.activity = "idle"
            self._beat()

    def _poll_child(self):
        if self.session is None or self.delivered:
            return
        try:
            state = self.launcher.state(self.session)
        except Exception as exc:
            self.log.error("Could not read session state: %s", exc)
            return
        if state in _RUNNING or state is None:
            return
        if state in _FINISHED_OK:
            self._mirror_device_log(self.session.folder)
            self.log.info("Recording process finished; delivering %s", self.session.folder)
            self._deliver(self.session.folder)
            self.delivered = True
            self.activity = "idle"
            return
        if state in _FINISHED_BAD:
            self.log.error("Recording process ended (%s) before the call ended.", state)
            self.delivered = True
            self.activity = "idle"

    def _wait_until_finished(self, session):
        deadline = self.monotonic() + float(self.config.transcribe_timeout_seconds)
        while True:
            self._beat()
            try:
                state = self.launcher.state(session)
            except Exception as exc:
                self.log.error("Could not read session state: %s", exc)
                return "failed"
            if state in _FINISHED_OK or state in _FINISHED_BAD:
                return state
            if self.monotonic() >= deadline:
                self.log.error("Timed out waiting for transcription of %s", session.folder)
                return "timeout"
            self.sleep(min(0.5, float(self.config.poll_interval_seconds) or 0.5))

    def _mirror_device_log(self, folder):
        """Copy new recorder device lines into the watcher log."""
        if not folder:
            return
        path = os.path.join(folder, DEVICE_LOG_FILENAME)
        offset = self._device_log_offsets.get(folder, 0)
        try:
            lines, offset = read_new_device_events(path, offset)
        except Exception as exc:
            self.log.error("Could not read the audio device log: %s", exc)
            return
        self._device_log_offsets[folder] = offset
        for line in lines:
            self.log.info("recorder: %s", line)

    def _deliver(self, folder):
        try:
            ok = self.deliver_fn(folder)
        except Exception as exc:
            from meeting_notes.deliver import redact_secret

            secret = getattr(self.config, "webhook_auth_value", "")
            self.log.error(
                "Delivery failed for %s: %s",
                folder,
                redact_secret(str(exc), secret),
            )
            return
        if ok:
            self.log.info("Delivered %s", folder)
        else:
            self.log.error("Delivery did not succeed for %s.", folder)

    def _retry_outbox(self):
        try:
            count = self.outbox_retry()
        except Exception as exc:
            self.log.error("Outbox retry failed: %s", exc)
            return
        if count:
            self.log.info("Retried outbox; delivered %s session(s).", count)


def configure_watch_logging(control_dir, max_bytes=1_000_000, backup_count=3):
    """Log to ~/.meeting-notes/watch.log and rotate so the file stays bounded."""
    os.makedirs(control_dir, exist_ok=True)
    path = os.path.join(control_dir, LOG_FILENAME)
    logger = logging.getLogger("meeting_notes.watch")
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.propagate = False
    file_handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    return logger, path


def build_windows_detector(config, log=None):
    from meeting_notes.call_sources import build_call_detector
    from meeting_notes.meet_detect_windows import list_top_level_windows, probe_microphone

    return build_call_detector(
        config,
        windows=list_top_level_windows,
        probe_microphone=probe_microphone,
        log=log,
    )


def _start_health_ping(config, previous, log):
    """Send the optional health ping without delaying call detection."""
    if not getattr(config, "watcher_health_ping", False) or not previous:
        return

    def _run():
        try:
            import time

            maybe_send_health(config, previous, time.time(), log=log)
        except Exception:
            log.exception("Unhandled exception during watcher-health ping")

    threading.Thread(target=_run, name="watcher-health-ping", daemon=True).start()


def serve(config, control_dir, output_dir, sleep=None, monotonic=None):
    """Run the watcher until Ctrl+C. Windows detection only.

    A second process exits immediately when one watcher already holds the lock.
    """
    import time

    if sys.platform != "win32":
        raise RuntimeError(
            "meeting-notes watch requires Windows "
            "(Chrome window enumeration and the microphone probes)."
        )
    logger, _path = configure_watch_logging(control_dir)
    lock = acquire_instance_lock(control_dir)
    if lock is None:
        owner = read_instance(control_dir) or {}
        logger.warning(
            "Watcher not started: another watcher holds the lock (pid %s). Exiting.",
            owner.get("pid"),
        )
        logger.info(
            "Watcher stopped: pid=%s reason=another watcher holds the lock",
            os.getpid(),
        )
        other = owner.get("pid")
        suffix = f" (pid {other})" if other else ""
        print(
            f"Another watcher is already running{suffix}. This process is exiting.",
            file=sys.stderr,
        )
        return

    reason = consume_start_reason(control_dir)
    version = package_version()
    started = time.time()
    write_instance(control_dir, os.getpid(), version, started)
    write_watcher_pid(control_dir)
    # Read the previous beat before this process overwrites it. That gap is
    # how long the watcher was down.
    previous_heartbeat = read_heartbeat(control_dir)
    heartbeat = Heartbeat(
        control_dir,
        interval=HEARTBEAT_INTERVAL_SECONDS,
        version=version,
    )
    stop_reason = "stopped before the loop started"
    try:
        try:
            active = read_watcher_recording(control_dir)
            if active:
                heartbeat.beat(
                    force=True,
                    state="recording",
                    session_folder=active.get("folder") or active.get("folder_name"),
                    session_pid=active.get("pid"),
                )
            else:
                heartbeat.beat(force=True, state="idle")
        except Exception:
            logger.exception("Could not write the heartbeat")
        logger.info(
            "Watcher starting: pid=%s version=%s reason=%s",
            os.getpid(),
            version,
            reason,
        )
        _start_health_ping(config, previous_heartbeat, logger)
        stop_event = threading.Event()

        def _handle_sigint(_sig, _frame):
            stop_event.set()
            logger.info("Stop requested; finishing the current session if one is recording.")

        previous_signal = None
        try:
            import signal

            previous_signal = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _handle_sigint)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, _handle_sigint)
        except Exception:
            previous_signal = None

        def _sleep(seconds):
            fn = sleep or time.sleep
            if stop_event.is_set():
                return
            # Wake promptly on Ctrl+C without busy-spinning.
            end = (monotonic or time.monotonic)() + seconds
            while (monotonic or time.monotonic)() < end:
                if stop_event.is_set():
                    return
                remaining = end - (monotonic or time.monotonic)()
                fn(min(0.2, remaining))

        box = outbox_dir(control_dir)

        def _deliver(folder):
            return deliver_folder(folder, config, control_dir=control_dir, log=logger)

        def _retry():
            return retry_outbox(box, config, log=logger)

        watcher = MeetWatcher(
            config=config,
            detector=build_windows_detector(config, log=logger),
            launcher=SubprocessLauncher(control_dir),
            deliver_fn=_deliver,
            lock_store=FileLockStore(control_dir),
            outbox_retry=_retry,
            sleep=_sleep,
            monotonic=monotonic or time.monotonic,
            wall_iso=iso_now,
            wall_datetime=_wall_datetime,
            output_dir=output_dir,
            log=logger,
            heartbeat=heartbeat,
        )
        logger.info(
            "Watching for %s (join %.0fs, leave %.0fs). Ctrl+C to stop.",
            _watched_sources(config),
            config.join_debounce_seconds,
            config.leave_debounce_seconds,
        )
        stop_reason = "stop requested"
        try:
            watcher.run(stop_event=stop_event)
        except Exception:
            stop_reason = "unhandled exception"
            logger.exception("Unhandled exception in watcher")
            raise
        finally:
            try:
                import signal

                if previous_signal is not None:
                    signal.signal(signal.SIGINT, previous_signal)
            except Exception:
                pass
    finally:
        try:
            heartbeat.beat(force=True, state="idle")
        except Exception:
            logger.exception("Could not write the final heartbeat")
        clear_watcher_pid(control_dir, pid=os.getpid())
        clear_instance(control_dir, pid=os.getpid())
        logger.info("Watcher stopped: pid=%s reason=%s", os.getpid(), stop_reason)
        lock.release()


def _watched_sources(config):
    """Human list of enabled sources, for the startup log line."""
    enabled = []
    if getattr(config, "watch_google_meet", True):
        enabled.append("Google Meet")
    if getattr(config, "watch_slack_huddle", True):
        enabled.append("Slack Huddle")
    if not enabled:
        return "no call sources (both are disabled in config)"
    if len(enabled) == 1:
        return enabled[0]
    return "%s and %s" % (enabled[0], enabled[1])


def _wall_datetime():
    from datetime import datetime

    return datetime.now().astimezone()
