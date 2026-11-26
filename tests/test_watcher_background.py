"""Back-to-back calls, crash resilience, and background resume."""

import os
import threading
import time
from datetime import datetime, timezone

from meeting_notes.config import Config
from meeting_notes.meet_detect import Sighting
from meeting_notes.session import write_status
from meeting_notes.session_finish import unfinished_folders
from meeting_notes.watcher import (
    MeetWatcher,
    RunningSession,
    build_process_argv,
    interruptible_sleep,
    run_forever,
)


def sight(code):
    return Sighting(
        key=code,
        display_name=code,
        meet_code=code,
        window_title="Meet - %s - Google Chrome" % code,
    )


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        assert seconds >= 0
        self.t += float(seconds)


class Log:
    def __init__(self):
        self.messages = []

    def _add(self, level, msg, args):
        try:
            text = msg % args if args else str(msg)
        except Exception:
            text = "%s %s" % (msg, args)
        self.messages.append((level, text))

    def info(self, msg, *args):
        self._add("info", msg, args)

    def warning(self, msg, *args):
        self._add("warning", msg, args)

    def error(self, msg, *args):
        self._add("error", msg, args)

    def text(self):
        return "\n".join(m for _l, m in self.messages)


class Lock:
    def current(self):
        return None


def config():
    return Config(
        join_debounce_seconds=0,
        leave_debounce_seconds=0,
        poll_interval_seconds=1,
        outbox_retry_seconds=10**9,
        transcribe_timeout_seconds=10**6,
        watcher_transcript_only=True,
        whisper_model="small",
        language="en",
    )


def make_watcher(tmp_path, detector, launcher, clock, delivered, **extra):
    return MeetWatcher(
        config=config(),
        detector=detector,
        launcher=launcher,
        deliver_fn=lambda folder: delivered.append(folder) or True,
        lock_store=Lock(),
        outbox_retry=lambda: 0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-11-17T14:30:00-05:00",
        wall_datetime=lambda: datetime(2026, 11, 17, 19, 30, tzinfo=timezone.utc),
        output_dir=str(tmp_path),
        log=Log(),
        **extra,
    )


class Script:
    """Detector that returns the sighting scripted for each clock time."""

    def __init__(self, clock, timeline):
        self.clock = clock
        self.timeline = timeline  # list of (until_t, sighting or None)

    def poll(self, in_call=False):
        for until, sighting in self.timeline:
            if self.clock.t < until:
                return sighting
        return None


class Launcher:
    """Each session stays 'transcribing' until released."""

    def __init__(self):
        self.started = []
        self.stopped = []
        self.release = set()

    def start(self, spec):
        self.started.append(spec)
        return RunningSession(folder=spec.folder, pid=1000 + len(self.started))

    def request_stop(self, session):
        self.stopped.append(session.folder)

    def state(self, session):
        if session.folder in self.stopped:
            return "done" if session.folder in self.release else "transcribing"
        return "recording"


def test_back_to_back_call_is_recorded_while_the_last_one_still_transcribes(tmp_path):
    clock = Clock()
    # Call A to t=3, a one-tick gap, call B from t=4 on.
    detector = Script(clock, [(3, sight("aaa-aaaa-aaa")), (4, None), (100, sight("bbb-bbbb-bbb"))])
    launcher = Launcher()
    delivered = []
    watcher = make_watcher(tmp_path, detector, launcher, clock, delivered)
    watcher.run(max_ticks=7)

    assert len(launcher.started) == 2
    first, second = (spec.folder for spec in launcher.started)
    assert first != second
    assert launcher.stopped == [first]
    # A is still transcribing, B is recording, nothing was lost or delivered.
    assert delivered == []
    assert watcher.session is not None and watcher.session.folder == second
    assert [e["session"].folder for e in watcher._finishing] == [first]

    launcher.release.add(first)
    watcher.tick()
    assert delivered == [first]
    assert watcher._finishing == []


def test_interruptible_sleep_never_passes_a_negative_length():
    seen = []

    def sleep_fn(seconds):
        assert seconds >= 0, "sleep length must be non-negative"
        seen.append(seconds)

    ticks = iter([0.0, 0.0, 0.5, 1.0, 1.0, 2.0])
    # The clock jumps past the deadline between reads: remaining goes negative.
    interruptible_sleep(1.0, threading.Event(), sleep_fn, lambda: next(ticks))
    assert all(s >= 0 for s in seen)
    interruptible_sleep(-5, threading.Event(), sleep_fn, lambda: 0.0)


def test_exception_in_tick_does_not_end_the_loop(tmp_path):
    clock = Clock()

    class Flaky:
        def __init__(self):
            self.calls = 0

        def poll(self, in_call=False):
            self.calls += 1
            if self.calls in (2, 3):
                raise RuntimeError("boom")
            return None

    detector = Flaky()
    watcher = make_watcher(tmp_path, detector, Launcher(), clock, [])
    watcher.run(max_ticks=6)
    assert detector.calls == 6
    assert watcher.log.text().count("Watcher tick failed") == 2
    assert watcher.tick_errors == 0  # recovered


def test_a_failing_sleep_does_not_end_the_loop(tmp_path):
    clock = Clock()
    calls = {"n": 0}

    def bad_sleep(seconds):
        calls["n"] += 1
        raise ValueError("sleep length must be non-negative")

    watcher = make_watcher(tmp_path, Script(clock, []), Launcher(), clock, [])
    watcher.sleep = bad_sleep
    watcher.run(max_ticks=3)
    assert calls["n"] == 3


def test_run_forever_restarts_the_loop_after_an_exception():
    stop = threading.Event()
    log = Log()

    class W:
        runs = 0

        def run(self, stop_event=None):
            W.runs += 1
            if W.runs == 1:
                raise RuntimeError("unexpected")
            stop.set()

    run_forever(W(), stop, log, pause=0, sleep=lambda _s: None)
    assert W.runs == 2
    assert "Unhandled exception in watcher" in log.text()


class Proc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def _unfinished_folder(root, name="2026-11-17_mox-qvzc-oab_1333"):
    folder = os.path.join(str(root), name)
    os.makedirs(folder)
    with open(os.path.join(folder, "audio.wav"), "wb") as handle:
        handle.write(b"RIFF" + b"\0" * 64)
    write_status(folder, {"state": "failed", "mode": "watch", "pid": 999999})
    return folder


def test_startup_resume_is_queued_in_the_background_and_one_at_a_time(tmp_path):
    f1 = _unfinished_folder(tmp_path, "2026-11-17_aaa_1000")
    f2 = _unfinished_folder(tmp_path, "2026-11-17_bbb_1100")
    clock = Clock()
    spawned = []
    procs = []

    def resumer(folder):
        spawned.append(folder)
        proc = Proc()
        procs.append(proc)
        return proc

    watcher = make_watcher(tmp_path, Script(clock, []), Launcher(), clock, [], resumer=resumer)
    watcher.run(max_ticks=0)
    assert spawned == []  # startup did not transcribe anything inline
    watcher.tick()
    assert spawned == [f1]
    watcher.tick()
    assert spawned == [f1]  # second waits for the first
    procs[0].code = 0
    watcher.tick()
    assert spawned == [f1, f2]
    procs[1].code = 0
    watcher.tick()
    watcher.tick()
    assert spawned == [f1, f2]  # a finished folder is not queued again


def test_calls_are_still_detected_while_a_resume_runs(tmp_path):
    _unfinished_folder(tmp_path)
    clock = Clock()
    detector = Script(clock, [(100, sight("ccc-cccc-ccc"))])
    launcher = Launcher()
    watcher = make_watcher(tmp_path, detector, launcher, clock, [], resumer=lambda f: Proc())
    watcher.run(max_ticks=3)
    assert len(launcher.started) == 1
    assert watcher._resume_running is not None


def test_folder_being_transcribed_by_a_live_process_is_not_resumed(tmp_path):
    folder = _unfinished_folder(tmp_path)
    write_status(folder, {"state": "transcribing", "pid": os.getpid()})
    assert unfinished_folders(str(tmp_path)) == []
    write_status(folder, {"state": "transcribing", "pid": 999999})
    assert unfinished_folders(str(tmp_path)) == [folder]


def test_process_argv_is_niced_on_posix(monkeypatch):
    import meeting_notes.watcher as w

    monkeypatch.setattr(w.sys, "platform", "darwin")
    monkeypatch.setattr(w.shutil, "which", lambda _n: "/usr/bin/nice")
    argv = build_process_argv("/f", executable="/py")
    assert argv == ["/usr/bin/nice", "-n", "10", "/py", "-m", "meeting_notes.cli", "process", "/f"]


def test_watchdog_restarts_a_dead_watcher_without_a_wake_event():
    from meeting_notes.launchd_agent import start_watchdog

    ensured = []
    state = {"dead": True}

    def ensure(reason):
        ensured.append(reason)
        state["dead"] = False
        return "started"

    stop_holder = []

    def nap(_interval):
        time.sleep(0.01)

    stop = start_watchdog(
        "/nonexistent", ensure, Log(), interval=0, needs_start=lambda: state["dead"], sleep=nap
    )
    deadline = time.time() + 3
    while not ensured and time.time() < deadline:
        time.sleep(0.01)
    stop()
    assert ensured == ["launchd watchdog"]


def test_extension_dropout_does_not_split_a_call_into_a_second_session():
    """Nov 17: key switched from the meet code to the title slug mid-call."""
    from meeting_notes.meet_detect import CallDebouncer, ChromeMeetDetector, WindowInfo

    title = "Meet - Greek | Bebop - GTM call"
    state = {"extension": sight("zqx-pufq-xiq")}
    detector = ChromeMeetDetector(
        windows=lambda: [WindowInfo(title, "chrome.exe")],
        mic_in_use=lambda: True,
    )
    detector.extension_sighting = lambda: state["extension"]
    debouncer = CallDebouncer(0, 25)
    events = []
    now = 0.0
    for step in range(120):
        if step == 20:
            state["extension"] = None  # the extension heartbeat goes stale
        in_call = debouncer.phase in ("arming", "active", "dropping")
        found = detector.poll(in_call=in_call)
        events += debouncer.update(found, now)
        now += 1.0
    assert events == ["join"]
    assert debouncer.active.key == "zqx-pufq-xiq"


def test_a_different_call_with_its_own_meet_code_is_still_a_new_call():
    from meeting_notes.meet_detect import CallDebouncer, ChromeMeetDetector, WindowInfo

    windows = {"list": [WindowInfo("Meet - aaa-aaaa-aaa - Google Chrome", "chrome.exe")]}
    detector = ChromeMeetDetector(windows=lambda: windows["list"], mic_in_use=lambda: True)
    debouncer = CallDebouncer(0, 5)
    events = []
    now = 0.0
    for step in range(40):
        if step == 10:
            windows["list"] = [WindowInfo("Meet - bbb-bbbb-bbb - Google Chrome", "chrome.exe")]
        found = detector.poll(in_call=debouncer.phase in ("arming", "active", "dropping"))
        events += debouncer.update(found, now)
        now += 1.0
    assert events.count("leave") == 1 and events.count("join") == 2


def test_recorder_child_frees_the_session_lock_before_it_transcribes(tmp_path, monkeypatch):
    """The lock must be gone while Whisper runs, or the watcher can't record the next call."""
    import meeting_notes.cli as cli
    import meeting_notes.session_finish as session_finish
    from meeting_notes.session import read_active

    control = tmp_path / "control"
    control.mkdir()
    out = tmp_path / "meetings"
    monkeypatch.setattr(cli, "CONTROL_DIR", str(control))
    monkeypatch.setattr(cli, "_install_stop_signal", lambda: threading.Event())
    monkeypatch.setattr(cli, "_restore_stop_signals", lambda _e: None)
    monkeypatch.setattr(cli, "_uses_meet_speaker_bridge", lambda _s: False)
    monkeypatch.setattr(cli, "_release_hotkey", lambda: None)
    monkeypatch.setattr(cli, "_wait_for_stop", lambda *a, **k: None)
    monkeypatch.setattr(cli, "stop_recording", lambda _h: None)
    niced = []
    monkeypatch.setattr(cli, "_lower_priority", lambda *a: niced.append(1))

    def begin(audio_path, _labels):
        with open(audio_path, "wb") as handle:
            handle.write(b"RIFF" + b"\0" * 64)
        return object()

    monkeypatch.setattr(cli, "_begin_recording", begin)
    seen = {}

    def fake_finish(folder, *a, **k):
        seen["lock"] = read_active(str(control))
        seen["niced"] = bool(niced)
        return {"notes": None}

    monkeypatch.setattr(session_finish, "finish_folder", fake_finish)
    args = cli.build_parser().parse_args(
        ["start", "abc-defg-hij", "--output-dir", str(out / "2026-11-18_abc"), "--automated", "--transcript-only"]
    )
    cli.cmd_start(args)
    assert seen["lock"] is None
    assert seen["niced"] is True
