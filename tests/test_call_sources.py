from meeting_notes.call_sources import CompositeCallDetector, build_call_detector
from meeting_notes.config import Config
from meeting_notes.meet_detect import Sighting, WindowInfo


MEET = Sighting(
    key="abc-defg-hij",
    display_name="abc-defg-hij",
    meet_code="abc-defg-hij",
    window_title="Meet - abc-defg-hij - Google Chrome",
    source="google-meet",
)
HUDDLE_TITLE = "Huddle | #engineering | Acme - Slack"


class _ListLog:
    def __init__(self):
        self.messages = []

    def info(self, msg, *args):
        self.messages.append(msg % args if args else str(msg))

    def warning(self, msg, *args):
        self.messages.append(msg % args if args else str(msg))


class _Fake:
    def __init__(self, source, sighting):
        self.source = source
        self.sighting = sighting
        self.calls = []

    def poll(self, in_call=False):
        self.calls.append(in_call)
        value = self.sighting
        if callable(value):
            return value(in_call)
        return value


def test_idle_overlap_prefers_meet_and_an_active_call_is_not_stolen():
    log = _ListLog()
    meet = _Fake("google-meet", MEET)
    slack = _Fake("slack-huddle", Sighting(
        key="slack-huddle:engineering",
        display_name="#engineering",
        meet_code="",
        window_title=HUDDLE_TITLE,
        source="slack-huddle",
    ))
    detector = CompositeCallDetector([meet, slack], log=log)

    first = detector.poll(in_call=False)
    assert first.source == "google-meet"
    assert any("Recording google-meet" in message for message in log.messages)

    again = detector.poll(in_call=True)
    assert again.source == "google-meet"
    assert meet.calls[-1] is True
    assert slack.calls[-1] is False
    assert any("Staying with google-meet" in message for message in log.messages)

    meet.sighting = None
    assert detector.poll(in_call=True) is None
    resumed = detector.poll(in_call=False)
    assert resumed.source == "slack-huddle"


def test_source_flags_select_which_detector_runs():
    windows = [
        WindowInfo("Meet - abc-defg-hij - Google Chrome", "chrome.exe"),
        WindowInfo(HUDDLE_TITLE, "slack.exe"),
    ]
    probed = []

    def probe(marker):
        probed.append(marker)
        return True

    both = build_call_detector(
        Config(),
        windows=lambda: windows,
        probe_microphone=probe,
        log=_ListLog(),
    )
    assert both.poll().source == "google-meet"

    slack_only = build_call_detector(
        Config(watch_google_meet=False, watch_slack_huddle=True),
        windows=lambda: windows,
        probe_microphone=probe,
        log=_ListLog(),
    )
    sighting = slack_only.poll()
    assert sighting.source == "slack-huddle"
    assert sighting.display_name == "#engineering"

    meet_only = build_call_detector(
        Config(watch_google_meet=True, watch_slack_huddle=False),
        windows=lambda: windows,
        probe_microphone=probe,
        log=_ListLog(),
    )
    assert meet_only.poll().meet_code == "abc-defg-hij"

    neither = build_call_detector(
        Config(watch_google_meet=False, watch_slack_huddle=False),
        windows=lambda: windows,
        probe_microphone=probe,
        log=_ListLog(),
    )
    assert neither.poll() is None
    assert "slack.exe" in probed
