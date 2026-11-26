"""Find the calendar event for a recording and return its attendees.

Two sources, both optional and read-only:

- `calendar_json_path`: a JSON file of events, e.g. one a Google Calendar sync
  drops into the Google Drive folder. Shape: `{"events": [{"title", "start",
  "end", "meet_code"?, "organizer"?, "attendees": [{"name", "email"}]}]}` with
  ISO-8601 times.
- `calendar_ics_url`: the calendar's private iCal address. Fetched over HTTPS
  with a short timeout; the URL is a secret and is never logged.

The event that overlaps the recording start (+-15 min) wins; a Meet code match
or a window-title match breaks ties.
"""

import datetime as dt
import json
import os
import re
import urllib.request

WINDOW = dt.timedelta(minutes=15)


def _parse_iso(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _event_entries(event):
    from meeting_notes.speaker_naming import clean_name, roster_entry

    entries = []
    for attendee in event.get("attendees") or []:
        if isinstance(attendee, str):
            attendee = {"name": attendee}
        if not isinstance(attendee, dict):
            continue
        if str(attendee.get("response") or attendee.get("responseStatus") or "").lower() == "declined":
            continue
        name = clean_name(attendee.get("name") or attendee.get("displayName"))
        email = attendee.get("email") or ""
        if name:
            entries.append(roster_entry(name, "calendar", 1.0, email))
        elif email:
            local = email.split("@")[0]
            guess = re.split(r"[._\-+0-9]", local)[0]
            if len(guess) >= 3:
                entries.append(roster_entry(guess.capitalize(), "calendar-email", 0.5, email))
    return entries


def load_json_events(path):
    path = os.path.expanduser(path or "")
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    events = data.get("events") if isinstance(data, dict) else data
    return [e for e in events or [] if isinstance(e, dict)]


def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_time(value, params):
    value = value.strip()
    try:
        if len(value) == 8:
            return dt.datetime.strptime(value, "%Y%m%d").astimezone()
        if value.endswith("Z"):
            return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        parsed = dt.datetime.strptime(value, "%Y%m%dT%H%M%S")
        tzid = re.search(r"TZID=([^;:]+)", params or "")
        if tzid:
            try:
                from zoneinfo import ZoneInfo

                return parsed.replace(tzinfo=ZoneInfo(tzid.group(1)))
            except Exception:
                pass
        return parsed.astimezone()
    except ValueError:
        return None


def parse_ics(text):
    """Minimal VEVENT parser: title, start, end, attendees, organizer, Meet code."""
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(text or ""), re.S):
        event = {"attendees": []}
        for line in block.splitlines():
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            key, _, params = head.partition(";")
            key = key.upper()
            if key == "SUMMARY":
                event["title"] = value.replace("\\,", ",").strip()
            elif key == "DTSTART":
                event["start_dt"] = _ics_time(value, params)
            elif key == "DTEND":
                event["end_dt"] = _ics_time(value, params)
            elif key in ("ATTENDEE", "ORGANIZER"):
                cn = re.search(r'CN="?([^";:]+)"?', params)
                email = value.replace("mailto:", "").replace("MAILTO:", "").strip()
                declined = "PARTSTAT=DECLINED" in params.upper()
                item = {"name": cn.group(1).strip() if cn else "", "email": email}
                if cn and cn.group(1).strip() == email:
                    item["name"] = ""
                if declined:
                    item["response"] = "declined"
                if key == "ORGANIZER":
                    event["organizer"] = item
                    if not any(a.get("email") == email for a in event["attendees"]):
                        event["attendees"].append(item)
                else:
                    event["attendees"] = [a for a in event["attendees"] if a.get("email") != email] + [item]
            elif key in ("LOCATION", "DESCRIPTION", "X-GOOGLE-CONFERENCE"):
                code = re.search(r"meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})", value)
                if code:
                    event["meet_code"] = code.group(1)
        events.append(event)
    return events


def fetch_ics(url, timeout=8):
    if not url or not url.startswith("https://"):
        return []
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return parse_ics(response.read().decode("utf-8", "replace"))
    except Exception:
        return []


def _title_score(title, window_title):
    a = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    b = set(re.findall(r"[a-z0-9]+", (window_title or "").lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def match_event(events, started_at, meet_code="", window_title=""):
    start = _parse_iso(started_at) if not isinstance(started_at, dt.datetime) else started_at
    if start is None:
        return None
    best = None
    best_key = None
    for event in events or []:
        begin = event.get("start_dt") or _parse_iso(event.get("start"))
        end = event.get("end_dt") or _parse_iso(event.get("end")) or (begin + dt.timedelta(hours=1) if begin else None)
        if begin is None or end is None:
            continue
        if not (begin - WINDOW <= start <= end + WINDOW):
            continue
        code_hit = bool(meet_code and event.get("meet_code") == meet_code)
        key = (code_hit, _title_score(event.get("title"), window_title), -abs((start - begin).total_seconds()))
        if best_key is None or key > best_key:
            best, best_key = event, key
    return best


def calendar_roster(status, config):
    """Roster entries and the matched event title for a session's status.json."""
    events = load_json_events(getattr(config, "calendar_json_path", "") or "")
    url = getattr(config, "calendar_ics_url", "") or ""
    if url:
        events = events + fetch_ics(url)
    if not events:
        return [], None
    event = match_event(
        events,
        status.get("recording_started_at") or status.get("joined_at") or status.get("created_at"),
        meet_code=status.get("meet_code") or "",
        window_title=status.get("window_title") or "",
    )
    if not event:
        return [], None
    return _event_entries(event), event.get("title")
