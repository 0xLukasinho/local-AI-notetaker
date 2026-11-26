"""Extension Meet presence, permission wording, and the health fields."""

import json
import urllib.request

from meeting_notes.meet_detect import ChromeMeetDetector, WindowInfo
from meeting_notes.meet_presence import (
    PresenceBook,
    PresenceServer,
    parse_presence,
    presence_port,
    sighting_from_presence,
)
from meeting_notes.permissions_report import (
    interpret_screen_report,
    permission_warning,
    screen_permission_for_platform,
)
from meeting_notes.responsible_spawn import disclaim_supported
from meeting_notes.watch_guard import Heartbeat, describe_heartbeat


def test_presence_port_is_the_sibling_of_the_speaker_bridge():
    assert presence_port(8765) == 8766
    assert presence_port(65535) == 65534


def test_in_call_heartbeat_is_a_meet_sighting_and_lobby_is_not():
    parsed = parse_presence(
        {
            "meet_code": "abc-defg-hij",
            "phase": "in-call",
            "in_call": True,
            "visible": False,
            "title": "Meet - abc-defg-hij",
        }
    )
    sighting = sighting_from_presence(parsed)
    assert sighting is not None
    assert sighting.meet_code == "abc-defg-hij"
    assert sighting.source == "google-meet"

    lobby = parse_presence({"meet_code": "abc-defg-hij", "phase": "lobby", "in_call": False})
    assert sighting_from_presence(lobby) is None


def test_stale_heartbeat_is_not_a_call_and_a_fresh_one_does_not_need_a_window():
    book = PresenceBook(fresh_seconds=10, clock=lambda: 100.0)
    book.update(
        {"meet_code": "abc-defg-hij", "phase": "in-call", "in_call": True},
        now=100.0,
    )
    assert book.sighting(now=109.0) is not None
    assert book.sighting(now=111.0) is None

    detector = ChromeMeetDetector(
        windows=lambda: [],
        mic_in_use=lambda: False,
    )
    detector.extension_sighting = lambda: book.sighting(now=105.0)
    found = detector.poll()
    assert found is not None
    assert found.meet_code == "abc-defg-hij"

    # Window title remains the secondary path when the extension is quiet.
    quiet = ChromeMeetDetector(
        windows=lambda: [WindowInfo("Meet - xyz-abcd-efg - Google Chrome", "chrome.exe")],
        mic_in_use=lambda: True,
    )
    assert quiet.poll() is not None
    assert quiet.poll().meet_code == "xyz-abcd-efg"


def test_presence_listener_accepts_loopback_posts_only(tmp_path):
    book = PresenceBook(clock=lambda: 50.0)
    server = PresenceServer(book, 0)
    server.start()
    try:
        url = "http://127.0.0.1:%s/presence" % server.port
        data = json.dumps(
            {"meet_code": "abc-defg-hij", "phase": "in-call", "in_call": True}
        ).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
        assert book.sighting(now=50.0).meet_code == "abc-defg-hij"
    finally:
        server.stop()


def test_screen_permission_wording_and_non_mac_status():
    assert interpret_screen_report({"screen_recording": True}) == "granted"
    assert interpret_screen_report({"screen_recording": False}) == "missing"
    assert interpret_screen_report({}) == "unknown"
    assert screen_permission_for_platform("linux") == "not-applicable"
    assert screen_permission_for_platform("win32") == "not-applicable"
    assert screen_permission_for_platform("darwin") is None
    warning = permission_warning("missing")
    assert "Meeting Audio Capture" in warning
    assert "permissions request" in warning
    assert permission_warning("granted") == ""
    assert permission_warning("not-applicable") == ""
    assert disclaim_supported("linux") is False
    assert disclaim_supported("darwin") is True


def test_heartbeat_records_screen_permission_and_extension_presence(tmp_path):
    heart = Heartbeat(str(tmp_path), now=lambda: 10.0, pid=4, version="0.1.0", iso=lambda _u: "t")
    heart.extra = lambda: {
        "screen_permission": "missing",
        "extension_presence": "never",
    }
    heart.beat(force=True, state="idle")
    text = describe_heartbeat(str(tmp_path), now=10.0)
    assert "screen_permission=missing" in text
    assert "extension_presence=never" in text


def test_presence_keeps_participant_names_and_puts_them_on_the_sighting():
    parsed = parse_presence(
        {
            "meet_code": "abc-defg-hij",
            "phase": "in-call",
            "in_call": True,
            "title": "Meet - Design Review",
            "participants": [
                {"name": "Alex Rivera", "self": True},
                {"name": "Jordan Blake"},
                "Jordan Blake",
                {"name": "  "},
            ],
        }
    )
    assert parsed["participants"] == ["Alex Rivera", "Jordan Blake"]
    sighting = sighting_from_presence(parsed)
    assert sighting.meet_code == "abc-defg-hij"
    assert sighting.participants == ("Alex Rivera", "Jordan Blake")


def test_in_call_presence_without_a_leave_button_still_counts_when_names_or_code_are_present():
    # The extension reports in-call from participant tiles even if the leave
    # button label has changed.
    parsed = parse_presence(
        {
            "meet_code": "abc-defg-hij",
            "phase": "in-call",
            "in_call": True,
            "participants": [{"name": "Priya Shah"}],
        }
    )
    assert parsed["in_call"] is True
    assert sighting_from_presence(parsed).participants == ("Priya Shah",)
