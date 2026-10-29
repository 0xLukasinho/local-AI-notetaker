"""Google Meet detection primitives that do not touch Windows APIs.

A call is "present" only when the caller has already combined both signals:
a top-level Chrome window titled like ``Meet - <code or title>`` AND the
microphone consent record showing Chrome is using the mic. The lobby page
fails the mic check and must be passed in as no sighting.

CallDebouncer turns that instantaneous signal into join/leave events with
separate hold times so a reload, a mic toggle, or a brief tab switch does
not split one call into two sessions.
"""

import re
from dataclasses import dataclass

from meeting_notes.session import slugify


_CHROME_SUFFIX = re.compile(
    r"\s+[-–—]\s+Google Chrome(?:\s.*)?$",
    re.IGNORECASE,
)
_MEET_TITLE = re.compile(
    r"^Meet\s*[-–—:]\s+(.+)$",
    re.IGNORECASE,
)
_MEET_CODE = re.compile(r"\b([a-z]{3}-[a-z]{4}-[a-z]{3})\b", re.IGNORECASE)


@dataclass(frozen=True)
class Sighting:
    key: str
    display_name: str
    meet_code: str
    window_title: str


@dataclass(frozen=True)
class WindowInfo:
    title: str
    process_name: str


def extract_meet_code(text):
    if not text:
        return None
    match = _MEET_CODE.search(text)
    if not match:
        return None
    return match.group(1).lower()


def parse_meet_window_title(title, title_regex=None):
    """Return a Sighting when `title` looks like a Meet window, else None.

    `title_regex` replaces the built-in ``Meet - ...`` pattern. When it has a
    capture group, that group is the display name; otherwise the whole match is.
    """
    if title is None:
        return None
    raw = str(title).strip()
    if not raw:
        return None
    if title_regex:
        try:
            match = re.search(title_regex, raw)
        except re.error:
            return None
        if not match:
            return None
        display = match.group(1) if match.groups() else match.group(0)
    else:
        cleaned = _CHROME_SUFFIX.sub("", raw).strip()
        match = _MEET_TITLE.match(cleaned)
        if not match:
            return None
        display = match.group(1)
    display = _CHROME_SUFFIX.sub("", display).strip()
    if not display:
        return None
    code = extract_meet_code(display)
    if code and display.lower() == code:
        display_name = code
    else:
        display_name = display
    key = code or slugify(display_name) or display_name.lower()
    return Sighting(
        key=key,
        display_name=display_name,
        meet_code=code,
        window_title=raw,
    )


def session_name(sighting):
    """Folder/meeting name: Meet code when we have one, otherwise the title."""
    if sighting.meet_code:
        return sighting.meet_code
    return sighting.display_name


def mic_in_use_from_entries(entries, marker="chrome.exe"):
    """True when a consent-store entry for `marker` has LastUsedTimeStop == 0.

    `entries` is a sequence of ``(subkey_name, last_used_time_stop)``. Windows
    writes 0 in LastUsedTimeStop while the app is using the microphone.
    """
    needle = (marker or "").lower()
    if not needle:
        return False
    for name, stop in entries:
        if needle not in str(name).lower():
            continue
        try:
            if int(stop) == 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


class ChromeMeetDetector:
    """Combine a window list and a mic probe. Both have to agree.

    `windows` returns WindowInfo items. `mic_in_use` returns bool. Either
    callable may be a fake in tests. Exceptions are treated as "not in a call"
    so a flaky probe cannot start a recording.
    """

    def __init__(self, windows, mic_in_use, title_regex=None, process_marker="chrome.exe"):
        self.windows = windows
        self.mic_in_use = mic_in_use
        self.title_regex = title_regex or None
        self.process_marker = process_marker or "chrome.exe"

    def poll(self):
        try:
            mic = bool(self.mic_in_use())
        except Exception:
            return None
        if not mic:
            return None
        try:
            windows = self.windows() or []
        except Exception:
            return None
        marker = self.process_marker.lower()
        for window in windows:
            process_name = (getattr(window, "process_name", "") or "").lower()
            if marker not in process_name:
                continue
            parsed = parse_meet_window_title(getattr(window, "title", ""), self.title_regex)
            if parsed is not None:
                return parsed
        return None


class CallDebouncer:
    """Join after a continuous sighting, leave after a continuous absence.

    Times are monotonic seconds supplied by the caller. A different Meet key
    counts as absence of the current call; the replacement starts its own join
    hold only after the leave hold completes.
    """

    def __init__(self, join_after, leave_after):
        self.join_after = float(join_after)
        self.leave_after = float(leave_after)
        self.phase = "idle"  # idle | arming | active | dropping
        self.pending_key = None
        self.pending_since = None
        self.pending = None
        self.active = None
        self.absent_since = None

    def update(self, sighting, now):
        """Return a list of 'join' and/or 'leave' events for this sample."""
        now = float(now)
        if self.phase in ("idle", "arming"):
            return self._update_idle(sighting, now)
        return self._update_active(sighting, now)

    def _update_idle(self, sighting, now):
        if sighting is None:
            self.phase = "idle"
            self.pending_key = None
            self.pending_since = None
            self.pending = None
            return []
        if self.pending_key != sighting.key:
            self.phase = "arming"
            self.pending_key = sighting.key
            self.pending_since = now
            self.pending = sighting
            return []
        self.pending = sighting
        if now - self.pending_since >= self.join_after:
            self.phase = "active"
            self.active = sighting
            self.absent_since = None
            self.pending_key = None
            self.pending_since = None
            self.pending = None
            return ["join"]
        return []

    def _update_active(self, sighting, now):
        same = (
            sighting is not None
            and self.active is not None
            and sighting.key == self.active.key
        )
        if same:
            self.phase = "active"
            self.absent_since = None
            self.active = sighting
            return []
        if self.absent_since is None:
            self.phase = "dropping"
            self.absent_since = now
            return []
        if now - self.absent_since < self.leave_after:
            return []
        self.active = None
        self.absent_since = None
        self.phase = "idle"
        events = ["leave"]
        if sighting is not None:
            self.phase = "arming"
            self.pending_key = sighting.key
            self.pending_since = now
            self.pending = sighting
        return events
