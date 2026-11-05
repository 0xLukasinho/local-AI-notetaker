from datetime import datetime, timezone

from meeting_notes.meet_detect import WindowInfo, session_name
from meeting_notes.slack_detect import (
    SlackHuddleDetector,
    browser_title_is_slack_huddle,
    parse_huddle_window_title,
)


def test_parse_channel_dm_and_bare_huddle_titles():
    cases = [
        ("Huddle | #engineering | Acme - Slack", "#engineering", False),
        ("Huddle - #engineering - Acme - Slack", "#engineering", False),
        ("Huddle with Jane Doe - Acme - Slack", "Jane Doe", False),
        ("Huddle in #design-review", "#design-review", False),
        ("Huddle | Jane Doe | Acme - Slack", "Jane Doe", False),
        ("Huddle | Jane Doe, Sam Rivera | Acme - Slack", "Jane Doe, Sam Rivera", False),
        ("(3) Huddle | #eng | Acme - Slack - Google Chrome", "#eng", False),
        ("Slack | Huddle | #engineering | Acme", "#engineering", False),
        ("Slack Huddle - #engineering - Acme - Slack", "#engineering", False),
        ("🎧 #engineering - Acme - Slack", "#engineering", False),
        ("Huddle", "Slack Huddle", True),
        ("Huddle | Huddle", "Slack Huddle", True),
        ("Huddle - Google Chrome", "Slack Huddle", True),
    ]
    for title, display, unnamed in cases:
        parsed = parse_huddle_window_title(title)
        assert parsed is not None, title
        assert parsed.display_name == display, title
        assert parsed.unnamed is unnamed, title
        assert parsed.source == "slack-huddle"
        assert parsed.meet_code == ""
        assert parsed.window_title == title
        if unnamed:
            assert parsed.key == "slack-huddle"
        else:
            assert parsed.key.startswith("slack-huddle:")


def test_parse_ignores_voice_messages_mic_tests_and_lookalike_titles():
    for title in (
        "",
        "#engineering - Acme - Slack",
        "#huddle - Acme - Slack",
        "Huddle-notes - Acme - Slack",
        "Preferences | Slack",
        "Use huddles in Slack | Slack",
        "What is a Huddle on Slack - Google Chrome",
        "Meet - abc-defg-hij - Google Chrome",
        "New message - Slack",
    ):
        assert parse_huddle_window_title(title) is None, title


def test_chrome_requires_a_slack_shaped_title():
    assert browser_title_is_slack_huddle(
        "Huddle | #engineering | Acme - Slack - Google Chrome"
    )
    assert browser_title_is_slack_huddle("Huddle - Google Chrome")
    assert browser_title_is_slack_huddle("Huddle with Jane Doe - Google Chrome")
    assert not browser_title_is_slack_huddle(
        "Huddle - Google Search - Google Chrome"
    )
    assert not browser_title_is_slack_huddle(
        "Huddle with your team: a field guide to meetings - Google Chrome"
    )
    assert not browser_title_is_slack_huddle(
        "#engineering - Acme - Slack - Google Chrome"
    )


def test_unnamed_session_name_adds_the_clock_and_a_channel_does_not():
    bare = parse_huddle_window_title("Huddle")
    named = parse_huddle_window_title("Huddle | #engineering | Acme - Slack")
    when = datetime(2026, 10, 29, 15, 4, tzinfo=timezone.utc)
    assert session_name(bare, when=when) == "Slack Huddle 15:04"
    assert session_name(named, when=when) == "#engineering"
    assert session_name(bare) == "Slack Huddle"


def _detector(windows, mic):
    state = {"windows": windows, "mic": mic, "probed": []}

    def read_windows():
        return state["windows"]

    def read_mic(marker):
        state["probed"].append(marker)
        value = state["mic"]
        if isinstance(value, dict):
            return value.get(marker, False)
        return value

    detector = SlackHuddleDetector(windows=read_windows, mic_in_use=read_mic)
    detector._state = state
    return detector


def test_desktop_huddle_requires_the_slack_microphone():
    title = "Huddle | #engineering | Acme - Slack"
    detector = _detector([WindowInfo(title, "slack.exe")], False)
    assert detector.poll() is None

    detector._state["mic"] = True
    sighting = detector.poll()
    assert sighting.display_name == "#engineering"
    assert sighting.source == "slack-huddle"
    assert detector._state["probed"] == ["slack.exe", "slack.exe"]


def test_voice_message_and_mic_test_do_not_match():
    detector = _detector(
        [
            WindowInfo("#engineering - Acme - Slack", "slack.exe"),
            WindowInfo("Preferences | Slack", "slack.exe"),
        ],
        True,
    )
    assert detector.poll() is None
    assert detector._state["probed"] == []


def test_chrome_huddle_uses_chrome_mic_and_rejects_a_search_page():
    search = _detector(
        [WindowInfo("Huddle - Google Search - Google Chrome", "chrome.exe")],
        {"chrome.exe": True},
    )
    assert search.poll() is None
    assert search._state["probed"] == []

    huddle = _detector(
        [WindowInfo("Huddle | #eng | Acme - Slack - Google Chrome", "chrome.exe")],
        {"chrome.exe": True, "slack.exe": False},
    )
    sighting = huddle.poll()
    assert sighting is not None
    assert sighting.display_name == "#eng"
    assert huddle._state["probed"] == ["chrome.exe"]


def test_desktop_window_wins_when_chrome_also_shows_a_huddle():
    detector = _detector(
        [
            WindowInfo("Huddle | #browser | Acme - Slack - Google Chrome", "chrome.exe"),
            WindowInfo("Huddle | #desktop | Acme - Slack", "slack.exe"),
        ],
        {"chrome.exe": True, "slack.exe": True},
    )
    assert detector.poll().display_name == "#desktop"


def test_in_call_survives_mute_and_a_missing_window():
    title = "Huddle | #engineering | Acme - Slack"
    detector = _detector([WindowInfo(title, "slack.exe")], True)
    assert detector.poll().display_name == "#engineering"

    detector._state["mic"] = False
    held = detector.poll(in_call=True)
    assert held is not None
    assert held.display_name == "#engineering"

    detector._state["mic"] = True
    detector._state["windows"] = [WindowInfo("#engineering - Acme - Slack", "slack.exe")]
    held = detector.poll(in_call=True)
    assert held is not None
    assert held.key == "slack-huddle:engineering"

    detector._state["mic"] = False
    assert detector.poll(in_call=True) is None
    # Not in a call: the latch must not resurrect a channel window or a
    # Huddle window whose microphone is idle.
    detector._state["windows"] = [WindowInfo("#engineering - Acme - Slack", "slack.exe")]
    detector._state["mic"] = True
    assert detector.poll(in_call=False) is None
    detector._state["windows"] = [WindowInfo(title, "slack.exe")]
    detector._state["mic"] = False
    assert detector.poll(in_call=False) is None


def test_bare_title_keeps_its_key_when_the_channel_appears():
    detector = _detector([WindowInfo("Huddle", "slack.exe")], True)
    first = detector.poll()
    assert first.unnamed is True
    assert first.key == "slack-huddle"

    detector._state["windows"] = [
        WindowInfo("Huddle | #engineering | Acme - Slack", "slack.exe")
    ]
    upgraded = detector.poll(in_call=True)
    assert upgraded.key == "slack-huddle"
    assert upgraded.display_name == "#engineering"
    assert upgraded.unnamed is False


def test_a_different_channel_is_not_the_same_huddle():
    detector = _detector(
        [WindowInfo("Huddle | #engineering | Acme - Slack", "slack.exe")],
        True,
    )
    detector.poll()
    detector._state["mic"] = False
    detector._state["windows"] = [
        WindowInfo("Huddle | #sales | Acme - Slack", "slack.exe")
    ]
    assert detector.poll(in_call=True) is None

    detector._state["mic"] = True
    switched = detector.poll(in_call=True)
    assert switched.display_name == "#sales"


def test_window_probe_error_is_logged_once():
    messages = []

    class Log:
        def info(self, msg, *args):
            messages.append(msg % args if args else msg)

        def warning(self, msg, *args):
            messages.append(msg % args if args else msg)

    detector = SlackHuddleDetector(
        windows=lambda: (_ for _ in ()).throw(OSError("enum failed")),
        mic_in_use=lambda marker: True,
        log=Log(),
    )
    assert detector.poll() is None
    assert detector.poll() is None
    assert messages.count("Slack window probe failed (enum failed)") == 1
