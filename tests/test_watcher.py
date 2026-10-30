import os
import sys
from datetime import datetime, timezone

from meeting_notes.config import Config
from meeting_notes.meet_detect import ChromeMeetDetector, Sighting, WindowInfo
from meeting_notes.watcher import (
    MeetWatcher,
    RunningSession,
    SessionSpec,
    build_start_argv,
    build_windows_detector,
    hidden_popen_kwargs,
)


SIGHT = Sighting(
    key="abc-defg-hij",
    display_name="abc-defg-hij",
    meet_code="abc-defg-hij",
    window_title="Meet - abc-defg-hij - Google Chrome",
)


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += float(seconds)


class ListLog:
    def __init__(self):
        self.messages = []

    def _fmt(self, msg, args):
        if not args:
            return str(msg)
        try:
            return msg % args
        except Exception:
            return f"{msg} {args}"

    def info(self, msg, *args):
        self.messages.append(("info", self._fmt(msg, args)))

    def warning(self, msg, *args):
        self.messages.append(("warning", self._fmt(msg, args)))

    def error(self, msg, *args):
        self.messages.append(("error", self._fmt(msg, args)))


class ScriptDetector:
    def __init__(self, clock, present_until):
        self.clock = clock
        self.present_until = present_until

    def poll(self, in_call=False):
        if self.clock.t < self.present_until:
            return SIGHT
        return None


class FakeLauncher:
    def __init__(self):
        self.started = []
        self.stopped = []
        self._state = "recording"

    def start(self, spec):
        self.started.append(spec)
        return RunningSession(folder=spec.folder, pid=4242)

    def request_stop(self, session):
        self.stopped.append(session.folder)
        self._state = "done"

    def state(self, _session):
        return self._state


class Lock:
    def __init__(self, current=None):
        self._current = current

    def current(self):
        return self._current


def _config():
    return Config(
        join_debounce_seconds=0,
        leave_debounce_seconds=0,
        poll_interval_seconds=1,
        outbox_retry_seconds=10**9,
        transcribe_timeout_seconds=30,
        watcher_transcript_only=True,
        whisper_model="small",
        language="en",
    )


def _watcher(tmp_path, detector, launcher, lock, delivered, retries):
    clock = detector.clock
    return MeetWatcher(
        config=_config(),
        detector=detector,
        launcher=launcher,
        deliver_fn=lambda folder: delivered.append(folder) or True,
        lock_store=lock,
        outbox_retry=lambda: retries.append("retry") or 0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-10-29T15:04:00+00:00",
        wall_datetime=lambda: datetime(2026, 10, 29, 15, 4, tzinfo=timezone.utc),
        output_dir=str(tmp_path),
        log=ListLog(),
    )


def test_watcher_starts_transcript_only_session_and_delivers_on_leave(tmp_path):
    clock = Clock()
    detector = ScriptDetector(clock, present_until=2)
    launcher = FakeLauncher()
    delivered = []
    retries = []
    watcher = _watcher(tmp_path, detector, launcher, Lock(), delivered, retries)
    watcher.run(max_ticks=4)

    assert retries[0] == "retry"
    assert len(launcher.started) == 1
    spec = launcher.started[0]
    assert spec.transcript_only is True
    assert spec.name == "abc-defg-hij"
    assert spec.meet_code == "abc-defg-hij"
    assert spec.window_title == SIGHT.window_title
    assert spec.joined_at == "2026-10-29T15:04:00+00:00"
    assert spec.whisper_model == "small"
    assert spec.folder.endswith("2026-10-29_abc-defg-hij")
    assert launcher.stopped == [spec.folder]
    assert delivered == [spec.folder]


def test_watcher_does_not_start_when_a_human_session_is_running(tmp_path):
    clock = Clock()
    detector = ScriptDetector(clock, present_until=2)
    launcher = FakeLauncher()
    delivered = []
    log = ListLog()
    clock_holder = detector
    watcher = MeetWatcher(
        config=_config(),
        detector=clock_holder,
        launcher=launcher,
        deliver_fn=lambda folder: delivered.append(folder) or True,
        lock_store=Lock(
            {
                "pid": 77,
                "mode": "manual",
                "folder": str(tmp_path / "human"),
            }
        ),
        outbox_retry=lambda: 0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-10-29T15:04:00+00:00",
        wall_datetime=lambda: datetime(2026, 10, 29, 15, 4, tzinfo=timezone.utc),
        output_dir=str(tmp_path),
        log=log,
    )
    watcher.run(max_ticks=4)
    assert launcher.started == []
    assert launcher.stopped == []
    assert delivered == []
    assert any("already running" in message and "manual" in message for _level, message in log.messages)


def test_watcher_copies_device_events_into_its_log(tmp_path):
    clock = Clock()
    detector = ScriptDetector(clock, present_until=100)
    launcher = FakeLauncher()
    log = ListLog()
    watcher = MeetWatcher(
        config=_config(),
        detector=detector,
        launcher=launcher,
        deliver_fn=lambda folder: True,
        lock_store=Lock(),
        outbox_retry=lambda: 0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-10-29T15:04:00+00:00",
        wall_datetime=lambda: datetime(2026, 10, 29, 15, 4, tzinfo=timezone.utc),
        output_dir=str(tmp_path),
        log=log,
    )
    watcher.tick()
    watcher.tick()
    assert launcher.started
    folder = launcher.started[0].folder
    path = os.path.join(folder, "audio-devices.log")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("capturing microphone 'Headset'\n")
        handle.write("capturing default output 'Speakers [Loopback]'\n")
    watcher.tick()
    text = "\n".join(message for _level, message in log.messages)
    assert "recorder: capturing microphone 'Headset'" in text
    assert "recorder: capturing default output 'Speakers [Loopback]'" in text
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("default output switched: 'Speakers")
    watcher.tick()
    text = "\n".join(message for _level, message in log.messages)
    assert "default output switched" not in text
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(" [Loopback]' -> 'Headphones [Loopback]'\n")
    watcher.tick()
    text = "\n".join(message for _level, message in log.messages)
    assert text.count("recorder: capturing microphone 'Headset'") == 1
    assert "recorder: default output switched: 'Speakers [Loopback]' -> 'Headphones [Loopback]'" in text


def test_build_start_argv_is_hidden_and_transcript_only():
    spec = SessionSpec(
        name="abc-defg-hij",
        folder="/sessions/2026-10-29_abc-defg-hij",
        meet_code="abc-defg-hij",
        window_title="Meet - abc-defg-hij - Google Chrome",
        joined_at="2026-10-29T15:04:00+00:00",
        transcript_only=True,
        whisper_model="medium",
        language="en",
    )
    argv = build_start_argv(spec, executable=r"C:\Python\pythonw.exe")
    assert argv[0] == r"C:\Python\pythonw.exe"
    assert argv[1:4] == ["-m", "meeting_notes.cli", "start"]
    assert "--transcript-only" in argv
    assert "--automated" in argv
    assert "--output-dir" in argv
    assert "/sessions/2026-10-29_abc-defg-hij" in argv
    assert "--meet-code" in argv
    assert "abc-defg-hij" in argv
    flags = hidden_popen_kwargs()
    if sys.platform == "win32":
        assert flags["creationflags"] & 0x00000200
        assert flags["creationflags"] & 0x08000000
    else:
        assert flags == {}


MEET_TITLE = "Meet - abc-defg-hij - Google Chrome"


class _SignalDetector:
    """ChromeMeetDetector whose mic/window flip after the joining poll."""

    def __init__(self, after_join):
        self.n = 0
        self.mic = True
        self.windows = [WindowInfo(MEET_TITLE, "chrome.exe")]
        self.after_join = after_join
        self.calls = []
        self.detector = ChromeMeetDetector(
            windows=lambda: self.windows,
            mic_in_use=lambda: self.mic,
        )

    def poll(self, in_call=False):
        # The first sample only arms the debouncer; the second confirms the
        # join. Flip the signals after that so the in-call hold is what we test.
        if self.n >= 2:
            self.after_join(self)
        self.n += 1
        self.calls.append(in_call)
        return self.detector.poll(in_call=in_call)


def _run_signal_watcher(tmp_path, after_join, leave_after):
    clock = Clock()
    detector = _SignalDetector(after_join)
    launcher = FakeLauncher()
    delivered = []
    config = _config()
    config.leave_debounce_seconds = leave_after
    watcher = MeetWatcher(
        config=config,
        detector=detector,
        launcher=launcher,
        deliver_fn=lambda folder: delivered.append(folder) or True,
        lock_store=Lock(),
        outbox_retry=lambda: 0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-10-29T15:04:00+00:00",
        wall_datetime=lambda: datetime(2026, 10, 29, 15, 4, tzinfo=timezone.utc),
        output_dir=str(tmp_path),
        log=ListLog(),
    )
    watcher.run(max_ticks=4)
    return detector, launcher, delivered


def test_mute_does_not_end_the_call_while_the_meet_window_is_open(tmp_path):
    def mute(detector):
        detector.mic = False

    detector, launcher, delivered = _run_signal_watcher(tmp_path, mute, leave_after=100)
    assert detector.calls[0] is False
    assert True in detector.calls[1:]
    assert len(launcher.started) == 1
    assert launcher.stopped == []
    assert delivered == []


def test_background_tab_does_not_end_the_call_while_the_mic_is_held(tmp_path):
    def switch_tab(detector):
        detector.windows = [WindowInfo("Slack - Google Chrome", "chrome.exe")]

    _detector, launcher, delivered = _run_signal_watcher(tmp_path, switch_tab, leave_after=100)
    assert len(launcher.started) == 1
    assert launcher.stopped == []
    assert delivered == []


def test_call_ends_when_the_meet_window_and_the_mic_are_both_gone(tmp_path):
    def drop_both(detector):
        detector.mic = False
        detector.windows = []

    _detector, launcher, delivered = _run_signal_watcher(tmp_path, drop_both, leave_after=0)
    assert len(launcher.started) == 1
    assert launcher.stopped == [launcher.started[0].folder]
    assert delivered == [launcher.started[0].folder]


def test_build_windows_detector_accepts_wasapi_when_the_registry_is_stale(monkeypatch):
    import meeting_notes.meet_detect_windows as windows

    monkeypatch.setattr(
        windows,
        "read_microphone_entries",
        lambda: [(r"C:#Program Files#Google#Chrome#Application#chrome.exe", 133)],
    )
    monkeypatch.setattr(
        windows,
        "read_wasapi_capture_sessions",
        lambda: [("chrome.exe", 1)],
    )
    monkeypatch.setattr(
        windows,
        "list_top_level_windows",
        lambda: [WindowInfo(MEET_TITLE, "chrome.exe")],
    )
    log = ListLog()
    detector = build_windows_detector(Config(), log=log)
    sighting = detector.poll()
    assert sighting is not None
    assert sighting.meet_code == "abc-defg-hij"
    text = " ".join(message for _level, message in log.messages)
    assert "registry=no" in text
    assert "wasapi=yes" in text
    assert "Meet window detected" in text


def test_build_windows_detector_keeps_the_registry_when_pycaw_is_missing(monkeypatch):
    import meeting_notes.meet_detect_windows as windows

    monkeypatch.setattr(
        windows,
        "read_microphone_entries",
        lambda: [(r"C:#Program Files#Google#Chrome#Application#chrome.exe", 0)],
    )

    def missing():
        raise ImportError("pycaw is not installed; pip install pycaw")

    monkeypatch.setattr(windows, "read_wasapi_capture_sessions", missing)
    monkeypatch.setattr(
        windows,
        "list_top_level_windows",
        lambda: [WindowInfo(MEET_TITLE, "chrome.exe")],
    )
    log = ListLog()
    detector = build_windows_detector(Config(), log=log)
    sighting = detector.poll()
    assert sighting is not None
    assert sighting.meet_code == "abc-defg-hij"
    assert any(
        level == "warning" and "pip install pycaw" in message for level, message in log.messages
    )
    assert any("registry=yes" in message and "wasapi=unavailable" in message for _level, message in log.messages)


def test_outbox_is_retried_when_the_watcher_starts(tmp_path):
    clock = Clock()
    retries = []
    watcher = MeetWatcher(
        config=_config(),
        detector=ScriptDetector(clock, present_until=-1),
        launcher=FakeLauncher(),
        deliver_fn=lambda _folder: True,
        lock_store=Lock(),
        outbox_retry=lambda: retries.append("retry") or 2,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-10-29T15:04:00+00:00",
        wall_datetime=lambda: datetime.now(timezone.utc),
        output_dir=str(tmp_path),
        log=ListLog(),
    )
    watcher.run(max_ticks=0)
    assert retries == ["retry"]
