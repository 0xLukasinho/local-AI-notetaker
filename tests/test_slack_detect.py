from datetime import datetime, timezone

from meeting_notes.meet_detect import CallDebouncer, WindowInfo, session_name
from meeting_notes.slack_detect import (
    SlackHuddleDetector,
    conversation_from_slack_title,
)


def test_titles_that_clearly_name_a_channel_or_dm():
    cases = [
        ("#engineering - Acme - Slack", "#engineering"),
        ("(3) #engineering - Acme - Slack", "#engineering"),
        ("Huddle | #engineering | Acme - Slack", "#engineering"),
        ("engineering (Channel) - Acme - Slack", "engineering"),
        ("Jane Doe (DM) - Acme - Slack", "Jane Doe"),
        ("Jane Doe, Sam Rivera (Group DM) - Acme - Slack", "Jane Doe, Sam Rivera"),
        ("engineering - Acme - Slack", "engineering"),
        ("Jane Doe - Acme - Slack", "Jane Doe"),
        ("🎧 #design-review - Acme - Slack", "#design-review"),
    ]
    for title, name in cases:
        assert conversation_from_slack_title(title) == name, title


def test_titles_that_do_not_name_a_conversation():
    for title in (
        "",
        "Slack",
        "(4) Slack",
        "Acme - Slack",
        "Preferences | Slack",
        "Activity - Acme - Slack",
        "New message - Slack",
        "Use huddles in Slack | Slack",
        "What is a Huddle on Slack - Google Chrome",
        "Meet - abc-defg-hij - Google Chrome",
        "Huddle - Google Search - Google Chrome",
    ):
        assert conversation_from_slack_title(title) is None, title


def test_unnamed_session_name_adds_the_clock_and_a_channel_does_not():
    when = datetime(2026, 10, 29, 15, 4, tzinfo=timezone.utc)
    bare = _detector([], True).poll()
    named = _detector(
        [WindowInfo("#engineering - Acme - Slack", "slack.exe")],
        True,
    ).poll()
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


def test_desktop_huddle_is_slack_exe_on_the_microphone():
    detector = _detector(
        [WindowInfo("#engineering - Acme - Slack", "slack.exe")],
        False,
    )
    assert detector.poll() is None

    detector._state["mic"] = True
    sighting = detector.poll()
    assert sighting is not None
    assert sighting.display_name == "#engineering"
    assert sighting.source == "slack-huddle"
    assert sighting.key == "slack-huddle"
    assert detector._state["probed"] == ["slack.exe", "slack.exe"]


def test_mic_alone_starts_a_huddle_with_no_window_and_a_generic_title():
    empty = _detector([], True)
    bare = empty.poll()
    assert bare.unnamed is True
    assert bare.display_name == "Slack Huddle"
    assert bare.key == "slack-huddle"

    generic = _detector([WindowInfo("Slack", "slack.exe")], True)
    sighting = generic.poll()
    assert sighting.unnamed is True
    assert sighting.display_name == "Slack Huddle"


def test_preferences_and_a_channel_window_use_the_channel_name():
    detector = _detector(
        [
            WindowInfo("Preferences | Slack", "slack.exe"),
            WindowInfo("Jane Doe - Acme - Slack", "slack.exe"),
        ],
        True,
    )
    sighting = detector.poll()
    assert sighting.display_name == "Jane Doe"
    assert sighting.unnamed is False


def test_chrome_mic_is_not_a_huddle():
    detector = _detector(
        [
            WindowInfo("#engineering - Acme - Slack - Google Chrome", "chrome.exe"),
            WindowInfo("app.slack.com/client/T123/C456 - Google Chrome", "chrome.exe"),
        ],
        {"chrome.exe": True, "slack.exe": False},
    )
    assert detector.poll() is None
    assert detector.poll(in_call=True) is None
    assert detector._state["probed"] == ["slack.exe", "slack.exe"]


def test_releasing_the_mic_ends_the_signal_while_the_window_stays_open():
    title = "#engineering - Acme - Slack"
    detector = _detector([WindowInfo(title, "slack.exe")], True)
    assert detector.poll().display_name == "#engineering"

    detector._state["mic"] = False
    assert detector.poll(in_call=True) is None
    assert detector.poll(in_call=False) is None


def test_a_generic_title_keeps_the_channel_already_seen_this_call():
    detector = _detector(
        [WindowInfo("#engineering - Acme - Slack", "slack.exe")],
        True,
    )
    assert detector.poll().display_name == "#engineering"
    detector._state["windows"] = [WindowInfo("Slack", "slack.exe")]
    held = detector.poll(in_call=True)
    assert held.key == "slack-huddle"
    assert held.display_name == "#engineering"
    assert held.unnamed is False


def test_a_new_huddle_does_not_keep_the_previous_channel():
    detector = _detector(
        [WindowInfo("#engineering - Acme - Slack", "slack.exe")],
        True,
    )
    detector.poll()
    detector._state["mic"] = False
    assert detector.poll(in_call=True) is None
    detector._state["mic"] = True
    detector._state["windows"] = [WindowInfo("Slack", "slack.exe")]
    fresh = detector.poll(in_call=False)
    assert fresh.unnamed is True
    assert fresh.display_name == "Slack Huddle"


def test_short_mic_use_does_not_join_and_release_leaves_after_the_hold():
    sighting = _detector([WindowInfo("Slack", "slack.exe")], True).poll()
    debouncer = CallDebouncer(join_after=12, leave_after=25)
    assert debouncer.update(sighting, 0) == []
    assert debouncer.update(sighting, 11) == []
    assert debouncer.update(None, 11.5) == []
    assert debouncer.phase == "idle"

    assert debouncer.update(sighting, 20) == []
    assert debouncer.update(sighting, 32) == ["join"]
    assert debouncer.update(None, 40) == []
    assert debouncer.update(None, 64.9) == []
    assert debouncer.update(None, 65) == ["leave"]


def test_window_probe_error_does_not_hide_the_microphone():
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
    first = detector.poll()
    second = detector.poll()
    assert first is not None and first.unnamed is True
    assert second is not None
    assert messages.count("Slack window probe failed (enum failed)") == 1
