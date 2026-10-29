import sys

import pytest

from meeting_notes.meet_detect import (
    CallDebouncer,
    ChromeMeetDetector,
    WindowInfo,
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
