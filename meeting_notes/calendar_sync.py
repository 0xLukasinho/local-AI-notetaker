"""Refresh calendar.json with upcoming events and attendees.

Used as a naming roster. Sources, in order:

- macOS Calendar (Calendar.app / EventKit via osascript), when requested
- a private ICS URL from config, already used at naming time

This module never logs attendee names. The JSON file lives outside the
repository (config calendar_json_path). Tests pass a fake runner.
"""

import datetime as dt
import json
import os
import re
import subprocess

from meeting_notes.atomic_io import atomic_write_json
from meeting_notes.calendar_roster import _parse_iso


# Meet codes in a location or notes field.
_MEET_CODE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b", re.IGNORECASE)


def _meet_code(event):
    for key in ("meet_code", "location", "url", "notes", "title"):
        text = event.get(key) or ""
        if not text:
            continue
        # Prefer the roster helper if the module has it; else local regex.
        match = _MEET_CODE.search(str(text))
        if match:
            return match.group(1).lower()
    return ""


def normalize_event(raw):
    """Return a roster event dict, or None if it has no usable window."""
    if not isinstance(raw, dict):
        return None
    title = " ".join(str(raw.get("title") or raw.get("summary") or "").split())
    start = raw.get("start") or raw.get("start_dt")
    end = raw.get("end") or raw.get("end_dt")
    if hasattr(start, "isoformat"):
        start = start.isoformat()
    if hasattr(end, "isoformat"):
        end = end.isoformat()
    begin = _parse_iso(start) if not isinstance(start, dt.datetime) else start
    finish = _parse_iso(end) if not isinstance(end, dt.datetime) else end
    if begin is None:
        return None
    if finish is None:
        finish = begin + dt.timedelta(hours=1)
    attendees = []
    seen = set()
    for item in raw.get("attendees") or raw.get("participants") or []:
        if isinstance(item, dict):
            name = " ".join(str(item.get("name") or item.get("displayName") or "").split())
            email = " ".join(str(item.get("email") or "").split())
        else:
            name = " ".join(str(item).split())
            email = ""
        # Skip the email if it is all we have; naming wants a display name.
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        attendees.append({"name": name, **({"email": email} if email else {})})
    event = {
        "title": title,
        "start": begin.isoformat(),
        "end": finish.isoformat(),
        "attendees": attendees,
    }
    code = raw.get("meet_code") or _meet_code({"title": title, "location": raw.get("location"), "url": raw.get("url"), "notes": raw.get("notes")})
    if code:
        event["meet_code"] = code
    if raw.get("location"):
        event["location"] = str(raw["location"])
    return event


def write_calendar_json(path, events):
    """Write the drop file consumed by calendar_roster. Returns the path."""
    if not path:
        raise ValueError("calendar_json_path is not set")
    payload = {
        "updated_at": dt.datetime.now().astimezone().isoformat(),
        "events": [normalize_event(event) for event in events],
    }
    payload["events"] = [event for event in payload["events"] if event]
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    atomic_write_json(path, payload)
    return path, len(payload["events"])


def _jxa_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "macos_calendar.jxa")


def macos_calendar_events(days=14, runner=None):
    """Upcoming Calendar.app events as dicts.

    `runner(argv) -> (returncode, stdout, stderr)`. Raises RuntimeError when
    Calendar access is denied or osascript fails.
    """
    days = int(days)
    if days < 1 or days > 60:
        raise ValueError("days must be between 1 and 60")
    script = _jxa_path()
    if not os.path.isfile(script):
        raise RuntimeError("macos_calendar.jxa is missing from the package")
    argv = ["osascript", "-l", "JavaScript", script, str(days)]
    if runner is None:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        code, out, err = completed.returncode, completed.stdout, completed.stderr
    else:
        code, out, err = runner(argv)
    text = (out or "").strip()
    if code not in (0, None):
        detail = (err or text or "").strip().splitlines()
        raise RuntimeError(
            "macOS Calendar could not be read. Grant Calendar access when "
            "prompted, or set calendar_ics_url in config.toml."
        )
    if not text:
        raise RuntimeError("macOS Calendar returned no data")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("macOS Calendar returned data that was not JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise RuntimeError("macOS Calendar access was not granted")
    return payload.get("events") or []


def sync_calendar(config, days=14, runner=None, include_ics=True):
    """Write calendar.json from macOS Calendar, plus the configured ICS URL."""
    path = getattr(config, "calendar_json_path", "") or ""
    if not path:
        raise ValueError("Set calendar_json_path in config.toml first")
    events = list(macos_calendar_events(days=days, runner=runner))
    if include_ics:
        url = getattr(config, "calendar_ics_url", "") or ""
        if url:
            from meeting_notes.calendar_roster import fetch_ics

            events.extend(fetch_ics(url) or [])
    return write_calendar_json(path, events)
