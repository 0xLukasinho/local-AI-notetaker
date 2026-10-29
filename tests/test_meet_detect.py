import sys

import pytest

from meeting_notes.meet_detect import (
    CallDebouncer,
    ChromeMeetDetector,
    WindowInfo,
    combine_mic_signals,
    mic_in_use_from_capture_sessions,
    mic_in_use_from_entries,
    parse_meet_window_title,
    session_name,
)


@pytest.mark.parametrize(
    "title,code,display",
    [
        ("Meet - abc-defg-hij", "abc-defg-hij", "abc-defg-hij"),
        ("Meet - abc-defg-hij - Google Chrome", "abc-defg-hij", "abc-defg-hij"),
        ("meet - ABC-DEFG-HIJ - Google Chrome", "abc-defg-hij", "abc-defg-hij"),
        ("Meet - Weekly Standup - Google Chrome", None, "Weekly Standup"),
        ("Meet - Weekly Standup - Google Chrome (Work)", None, "Weekly Standup"),
        ("Meet – Design Review", None, "Design Review"),
    ],
)
def test_parse_meet_titles(title, code, display):
    parsed = parse_meet_window_title(title)
    assert parsed is not None
    assert parsed.meet_code == code
    assert parsed.display_name == display
    assert parsed.window_title == title
    assert session_name(parsed) == (code or display)


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Google Meet",
        "Google Meet - Google Chrome",
        "Meet",
        "Meet - Google Chrome",
        "New Tab - Google Chrome",
        "Meeting Notes - Weekly - Google Chrome",
        "Speed Dial - Google Chrome",
    ],
)
def test_parse_ignores_lobby_and_unrelated_windows(title):
    assert parse_meet_window_title(title) is None


def test_title_regex_override():
    parsed = parse_meet_window_title(
        "Call: room-42 - Google Chrome",
        title_regex=r"Call:\s*(\S+)",
    )
    assert parsed is not None
    assert parsed.display_name == "room-42"
    assert parse_meet_window_title("Meet - abc-defg-hij", title_regex=r"Call: (\S+)") is None


def test_mic_entries_require_stop_zero_and_marker():
    entries = [
        (r"C:#Program Files#Google#Chrome#Application#chrome.exe", 0),
        (r"C:#Program Files#Mozilla Firefox#firefox.exe", 0),
    ]
    assert mic_in_use_from_entries(entries, "chrome.exe") is True
    assert mic_in_use_from_entries(
        [(r"C:#chrome.exe", 133000000000000000)],
        "chrome.exe",
    ) is False
    assert mic_in_use_from_entries(entries, "zoom.exe") is False


def test_detector_requires_chrome_window_and_mic():
    title = "Meet - abc-defg-hij - Google Chrome"
    windows = [WindowInfo(title, "chrome.exe"), WindowInfo("New Tab - Google Chrome", "chrome.exe")]

    lobby = ChromeMeetDetector(windows=lambda: windows, mic_in_use=lambda: False)
    assert lobby.poll() is None

    other_app = ChromeMeetDetector(
        windows=lambda: [WindowInfo(title, "firefox.exe")],
        mic_in_use=lambda: True,
    )
    assert other_app.poll() is None

    in_call = ChromeMeetDetector(windows=lambda: windows, mic_in_use=lambda: True)
    sighting = in_call.poll()
    assert sighting.meet_code == "abc-defg-hij"

    broken = ChromeMeetDetector(
        windows=lambda: (_ for _ in ()).throw(OSError("enum failed")),
        mic_in_use=lambda: True,
    )
    assert broken.poll() is None


def test_join_requires_a_continuous_hold_and_gap_resets():
    debouncer = CallDebouncer(join_after=12, leave_after=25)
    sighting = parse_meet_window_title("Meet - abc-defg-hij - Google Chrome")
    assert debouncer.update(sighting, 0) == []
    assert debouncer.update(sighting, 11.9) == []
    assert debouncer.update(None, 12) == []
    assert debouncer.phase == "idle"
    assert debouncer.update(sighting, 20) == []
    assert debouncer.update(sighting, 32) == ["join"]
    assert debouncer.phase == "active"


def test_leave_debounce_ignores_a_brief_dropout():
    debouncer = CallDebouncer(join_after=12, leave_after=25)
    sighting = parse_meet_window_title("Meet - Weekly Standup - Google Chrome")
    debouncer.update(sighting, 0)
    assert debouncer.update(sighting, 12) == ["join"]
    assert debouncer.update(None, 100) == []
    assert debouncer.update(sighting, 110) == []
    assert debouncer.phase == "active"
    assert debouncer.update(None, 200) == []
    assert debouncer.update(None, 224) == []
    assert debouncer.update(None, 225) == ["leave"]
    assert debouncer.phase == "idle"


def test_windows_probe_imports_on_any_os_and_refuses_to_run_elsewhere():
    import meeting_notes.meet_detect_windows as windows

    if sys.platform == "win32":
        return
    with pytest.raises(RuntimeError):
        windows.read_microphone_entries()
    with pytest.raises(RuntimeError):
        windows.read_wasapi_capture_sessions()


class _ListLog:
    def __init__(self):
        self.messages = []

    def _fmt(self, msg, args):
        if not args:
            return str(msg)
        return msg % args

    def info(self, msg, *args):
        self.messages.append(("info", self._fmt(msg, args)))

    def warning(self, msg, *args):
        self.messages.append(("warning", self._fmt(msg, args)))


def test_capture_sessions_require_active_state_and_marker():
    sessions = [("Chrome.exe", 1), ("zoom.exe", 1)]
    assert mic_in_use_from_capture_sessions(sessions, "chrome.exe") is True
    assert mic_in_use_from_capture_sessions([("chrome.exe", 0)], "chrome.exe") is False
    assert mic_in_use_from_capture_sessions([("chrome.exe", 2)], "chrome.exe") is False
    assert mic_in_use_from_capture_sessions([("chrome.exe", "nope")], "chrome.exe") is False
    assert mic_in_use_from_capture_sessions(sessions, "firefox.exe") is False
    assert mic_in_use_from_capture_sessions(sessions, "") is False


def test_combine_mic_signals_ors_registry_and_wasapi():
    stale = [(r"C:#chrome.exe", 99)]
    live = [(r"C:#chrome.exe", 0)]
    active = [("chrome.exe", 1)]
    idle = [("chrome.exe", 0)]

    both_off = combine_mic_signals("chrome.exe", stale, idle)
    assert both_off.in_use is False
    assert both_off.registry == "no"
    assert both_off.wasapi == "no"
    assert both_off.sources == ()

    wasapi_only = combine_mic_signals("chrome.exe", stale, active)
    assert wasapi_only.in_use is True
    assert wasapi_only.sources == ("wasapi",)
    assert wasapi_only.registry == "no"

    registry_only = combine_mic_signals("chrome.exe", live, idle)
    assert registry_only.sources == ("registry",)
    assert registry_only.wasapi == "no"

    both = combine_mic_signals("chrome.exe", live, active)
    assert both.sources == ("registry", "wasapi")

    missing_wasapi = combine_mic_signals(
        "chrome.exe",
        live,
        [],
        wasapi_available=False,
    )
    assert missing_wasapi.in_use is True
    assert missing_wasapi.sources == ("registry",)
    assert missing_wasapi.wasapi == "unavailable"

    neither = combine_mic_signals(
        "chrome.exe",
        [],
        [],
        registry_available=False,
        wasapi_available=False,
    )
    assert neither.in_use is False
    assert neither.registry == "unavailable"
    assert neither.wasapi == "unavailable"


def test_in_call_survives_mute_and_a_background_tab():
    title = "Meet - abc-defg-hij - Google Chrome"
    state = {"mic": True, "windows": [WindowInfo(title, "chrome.exe")]}
    detector = ChromeMeetDetector(
        windows=lambda: state["windows"],
        mic_in_use=lambda: state["mic"],
    )
    assert detector.poll().meet_code == "abc-defg-hij"

    state["mic"] = False
    held = detector.poll(in_call=True)
    assert held is not None
    assert held.meet_code == "abc-defg-hij"

    state["mic"] = True
    state["windows"] = [WindowInfo("Slack - Google Chrome", "chrome.exe")]
    held = detector.poll(in_call=True)
    assert held is not None
    assert held.meet_code == "abc-defg-hij"

    state["mic"] = False
    assert detector.poll(in_call=True) is None
    # Not in a call: a stale latch must not resurrect the meeting, and a Meet
    # window without the mic is still the lobby.
    state["mic"] = True
    state["windows"] = [WindowInfo("Slack - Google Chrome", "chrome.exe")]
    assert detector.poll(in_call=False) is None
    state["windows"] = [WindowInfo(title, "chrome.exe")]
    state["mic"] = False
    assert detector.poll(in_call=False) is None


def test_in_call_does_not_switch_to_another_meet_without_the_mic():
    first = "Meet - abc-defg-hij - Google Chrome"
    state = {"mic": True, "windows": [WindowInfo(first, "chrome.exe")]}
    detector = ChromeMeetDetector(
        windows=lambda: state["windows"],
        mic_in_use=lambda: state["mic"],
    )
    detector.poll()
    state["mic"] = False
    state["windows"] = [WindowInfo("Meet - Weekly Standup - Google Chrome", "chrome.exe")]
    assert detector.poll(in_call=True) is None

    state["mic"] = True
    switched = detector.poll(in_call=True)
    assert switched.display_name == "Weekly Standup"


def test_detector_logs_mic_source_and_window_changes_once():
    log = _ListLog()
    state = {"mic": combine_mic_signals("chrome.exe", [], []), "windows": []}
    detector = ChromeMeetDetector(
        windows=lambda: state["windows"],
        mic_in_use=lambda: state["mic"],
        log=log,
    )
    assert detector.poll() is None
    assert ("info", "Microphone not in use (registry=no, wasapi=no)") in log.messages
    assert ("info", "Meet window not detected") in log.messages

    log.messages.clear()
    state["mic"] = combine_mic_signals("chrome.exe", [(r"C:#chrome.exe", 50)], [("chrome.exe", 1)])
    state["windows"] = [WindowInfo("Meet - abc-defg-hij - Google Chrome", "chrome.exe")]
    assert detector.poll() is not None
    assert (
        "info",
        "Microphone in use (registry=no, wasapi=yes)",
    ) in log.messages
    assert any(
        level == "info" and message.startswith("Meet window detected:")
        for level, message in log.messages
    )

    log.messages.clear()
    assert detector.poll() is not None
    assert log.messages == []

    state["mic"] = combine_mic_signals("chrome.exe", [], [], wasapi_available=False, notice="pycaw missing")
    detector.poll(in_call=True)
    assert any("Microphone not in use" in message and "wasapi=unavailable" in message for _, message in log.messages)
    assert ("warning", "pycaw missing") in log.messages
    assert any("Holding the call because the Meet window is still open" in message for _, message in log.messages)

    log.messages.clear()
    detector.poll(in_call=True)
    assert log.messages == []


def test_window_probe_error_is_logged_once_and_does_not_raise():
    log = _ListLog()
    detector = ChromeMeetDetector(
        windows=lambda: (_ for _ in ()).throw(OSError("enum failed")),
        mic_in_use=lambda: True,
        log=log,
    )
    assert detector.poll() is None
    assert detector.poll() is None
    warnings = [message for level, message in log.messages if level == "warning"]
    assert warnings == ["Meet window probe failed (enum failed)"]


def test_capture_sessions_from_managers_reads_process_and_state():
    import meeting_notes.meet_detect_windows as windows

    class Control:
        def __init__(self, pid, state):
            self._pid = pid
            self._state = state

        def GetProcessId(self):
            return self._pid

        def GetState(self):
            return self._state

    class Enumerator:
        def __init__(self, controls):
            self._controls = controls

        def GetCount(self):
            return len(self._controls)

        def GetSession(self, index):
            return self._controls[index]

    class Manager:
        def __init__(self, controls):
            self._controls = controls

        def GetSessionEnumerator(self):
            return Enumerator(self._controls)

    class NeedsQuery:
        def __init__(self, inner):
            self._inner = inner

        def QueryInterface(self, _kind):
            return self._inner

    class BadManager:
        def GetSessionEnumerator(self):
            raise OSError("activate failed")

    names = {4: "chrome.exe", 5: "audiodg.exe"}
    sessions = windows.capture_sessions_from_managers(
        [
            Manager([Control(4, 1), NeedsQuery(Control(5, 1)), Control(0, 1)]),
            None,
            BadManager(),
        ],
        lambda pid: names[pid],
        control2_type=object,
    )
    assert sessions == [("chrome.exe", 1), ("audiodg.exe", 1), ("", 1)]
    assert mic_in_use_from_capture_sessions(sessions, "chrome.exe") is True


def test_read_wasapi_capture_sessions_enumerates_active_endpoints(monkeypatch):
    import meeting_notes.meet_detect_windows as windows

    class Device:
        def __init__(self, manager):
            self._manager = manager

        @property
        def AudioSessionManager(self):
            if self._manager == "bad":
                raise OSError("activate")
            return self._manager

    class Utils:
        @staticmethod
        def GetAllDevices(flow, state):
            assert flow == 1
            assert state == 1
            return [Device("bad"), Device("mgr-a")]

    monkeypatch.setattr(windows, "_require_windows", lambda: None)
    monkeypatch.setattr(windows, "_import_pycaw", lambda: (Utils, object, 1, 1))
    monkeypatch.setattr(windows, "_ensure_com", lambda: None)
    seen = {}

    def fake_sessions(managers, namer, control2_type):
        seen["managers"] = list(managers)
        seen["type"] = control2_type
        assert namer(0) == ""
        return [("chrome.exe", 1)]

    monkeypatch.setattr(windows, "capture_sessions_from_managers", fake_sessions)
    monkeypatch.setattr(windows, "_process_name", lambda pid: "chrome.exe" if pid else "")
    assert windows.read_wasapi_capture_sessions() == [("chrome.exe", 1)]
    assert seen["managers"] == ["mgr-a"]
    assert seen["type"] is object


def test_read_wasapi_raises_when_every_capture_device_fails(monkeypatch):
    import meeting_notes.meet_detect_windows as windows

    class Device:
        @property
        def AudioSessionManager(self):
            raise OSError("activate")

    class Utils:
        @staticmethod
        def GetAllDevices(_flow, _state):
            return [Device(), Device()]

    monkeypatch.setattr(windows, "_require_windows", lambda: None)
    monkeypatch.setattr(windows, "_import_pycaw", lambda: (Utils, object, 1, 1))
    monkeypatch.setattr(windows, "_ensure_com", lambda: None)
    with pytest.raises(OSError, match="capture session manager"):
        windows.read_wasapi_capture_sessions()


def test_missing_pycaw_is_an_import_error(monkeypatch):
    import builtins

    import meeting_notes.meet_detect_windows as windows

    real = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pycaw" or name.startswith("pycaw."):
            raise ImportError("blocked")
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(ImportError, match="pip install pycaw"):
        windows._import_pycaw()


def test_process_name_uses_psutil_and_swallows_errors(monkeypatch):
    import meeting_notes.meet_detect_windows as windows

    class Gone(Exception):
        pass

    class Psutil:
        @staticmethod
        def Process(pid):
            if pid == 3:
                raise Gone("exited")

            class Proc:
                def name(self):
                    return "chrome.exe"

            return Proc()

    monkeypatch.setitem(sys.modules, "psutil", Psutil)
    assert windows._process_name(0) == ""
    assert windows._process_name(3) == ""
    assert windows._process_name(9) == "chrome.exe"


def test_probe_microphone_degrades_when_wasapi_is_missing(monkeypatch):
    import meeting_notes.meet_detect_windows as windows

    monkeypatch.setattr(
        windows,
        "read_microphone_entries",
        lambda: [(r"C:#Program Files#Google#Chrome#Application#chrome.exe", 0)],
    )

    def missing():
        raise ImportError("pycaw is not installed; pip install pycaw")

    monkeypatch.setattr(windows, "read_wasapi_capture_sessions", missing)
    status = windows.probe_microphone("chrome.exe")
    assert status.in_use is True
    assert status.sources == ("registry",)
    assert status.wasapi == "unavailable"
    assert "pip install pycaw" in status.notice

    monkeypatch.setattr(
        windows,
        "read_microphone_entries",
        lambda: [(r"C:#chrome.exe", 133)],
    )
    monkeypatch.setattr(windows, "read_wasapi_capture_sessions", lambda: [("chrome.exe", 1)])
    status = windows.probe_microphone("chrome.exe")
    assert status.sources == ("wasapi",)
    assert status.notice == ""
    assert status.registry == "no"


def test_different_meeting_does_not_count_as_the_same_call():
    debouncer = CallDebouncer(join_after=10, leave_after=20)
    first = parse_meet_window_title("Meet - abc-defg-hij")
    second = parse_meet_window_title("Meet - Weekly Standup")
    debouncer.update(first, 0)
    debouncer.update(first, 10)
    assert debouncer.update(second, 50) == []
    assert debouncer.phase == "dropping"
    events = debouncer.update(second, 70)
    assert events == ["leave"]
    assert debouncer.phase == "arming"
    assert debouncer.update(second, 80) == ["join"]
