"""calendar.json refresh (fictional events only)."""

import json

from meeting_notes.calendar_sync import macos_calendar_events, normalize_event, sync_calendar, write_calendar_json


def test_normalize_event_keeps_attendees_and_a_meet_code():
    event = normalize_event(
        {
            "title": "Design Review",
            "start": "2026-11-25T15:00:00-05:00",
            "end": "2026-11-25T16:00:00-05:00",
            "location": "https://meet.google.com/abc-defg-hij",
            "attendees": [{"name": "Alex Rivera"}, "Jordan Blake", {"name": "Alex Rivera"}],
        }
    )
    assert event["title"] == "Design Review"
    assert event["meet_code"] == "abc-defg-hij"
    assert [item["name"] for item in event["attendees"]] == ["Alex Rivera", "Jordan Blake"]


def test_write_calendar_json_roundtrip(tmp_path):
    path = tmp_path / "calendar.json"
    out, count = write_calendar_json(
        str(path),
        [
            {
                "title": "Weekly Sync",
                "start": "2026-11-25T10:00:00-05:00",
                "end": "2026-11-25T10:30:00-05:00",
                "attendees": [{"name": "Priya Shah"}],
            }
        ],
    )
    assert count == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["events"][0]["attendees"][0]["name"] == "Priya Shah"
    assert "updated_at" in data


class _Cfg:
    def __init__(self, path, url=""):
        self.calendar_json_path = path
        self.calendar_ics_url = url


def test_sync_calendar_uses_a_fake_runner(tmp_path):
    payload = json.dumps(
        {
            "ok": True,
            "events": [
                {
                    "title": "Partners Check-in",
                    "start": "2026-11-26T09:00:00-05:00",
                    "end": "2026-11-26T09:30:00-05:00",
                    "attendees": [{"name": "Sam Ortega"}],
                }
            ],
        }
    )

    def runner(argv):
        assert argv[0] == "osascript"
        assert argv[-1] == "14"
        return 0, payload, ""

    path, count = sync_calendar(_Cfg(str(tmp_path / "calendar.json")), days=14, runner=runner, include_ics=False)
    assert count == 1
    data = json.loads((tmp_path / "calendar.json").read_text(encoding="utf-8"))
    assert data["events"][0]["attendees"][0]["name"] == "Sam Ortega"


def test_macos_calendar_denied_is_an_error():
    def runner(argv):
        return 1, "", "not authorized"

    try:
        macos_calendar_events(runner=runner)
    except RuntimeError as exc:
        assert "Calendar" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
