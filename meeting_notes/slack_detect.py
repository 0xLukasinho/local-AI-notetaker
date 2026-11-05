"""Slack Huddle detection that does not touch Windows APIs.

A huddle starts only when both signals agree, the same shape as Google Meet:

* a top-level window whose title is a Huddle window, and
* the microphone in use by that same process.

The desktop app is ``slack.exe``. Slack in Chrome is ``chrome.exe`` plus a
title that looks like the Slack app or a short Huddle popout, not an article
or a search page that mentions huddles. A normal channel window, a
voice-message recording, and the preferences microphone test do not match
the title, so they do not start a session.

Once the call is active, either signal keeps it. Muting can release the
capture session. The title can also drop for a moment while the Huddle
window is still the call. See ``SlackHuddleDetector.poll``.
"""

import re

from meeting_notes.meet_detect import MicStatus, Sighting
from meeting_notes.session import slugify


SOURCE_SLACK_HUDDLE = "slack-huddle"
SLACK_HUDDLE_NAME = "Slack Huddle"

# Substrings matched against the window's process basename.
DESKTOP_MARKER = "slack.exe"
BROWSER_MARKER = "chrome.exe"
PROCESS_MARKERS = (DESKTOP_MARKER, BROWSER_MARKER)

_BROWSER_SUFFIX = re.compile(
    r"\s+[-–—]\s+(?:Google Chrome|Mozilla Firefox|Microsoft Edge)(?:\s.*)?$",
    re.IGNORECASE,
)
_UNREAD = re.compile(r"^(?:\(\d+\)\s*)+")
_SLACK_APP_SUFFIX = re.compile(r"\s*(?:\||[-–—])\s*Slack\s*$", re.IGNORECASE)
# "Huddle", "Huddle | …", "Huddle - …", "Huddle: …", "Huddle with …",
# "Huddle in …". A hyphen glued to the word ("Huddle-notes", "#huddle") does
# not match. A leading headphones mark is the other title form.
_HUDDLE_START = re.compile(
    r"^(?:🎧\s*)?Huddle(?:\s*$|\s+(?:with|in)\b|\s*[|–—:]\s*|\s+[-–—]\s+)",
    re.IGNORECASE,
)
_HEADPHONES_START = re.compile(r"^🎧\s*\S")
_WITH_OR_IN = re.compile(r"^Huddle\s+(?:with|in)\s+(.+)$", re.IGNORECASE)
_LEADING_HUDDLE = re.compile(
    r"^Huddle(?:\s*[|–—:]\s*|\s+[-–—]\s+)",
    re.IGNORECASE,
)
_BARE_BROWSER_TITLE = re.compile(
    r"^(?:🎧\s*)?Huddle(?:\s*\|\s*Huddle)?\s*$",
    re.IGNORECASE,
)

_UNSEEN = object()


def marker_for_process(process_name):
    """Return the mic marker for a window process, or None."""
    name = (process_name or "").lower()
    if not name:
        return None
    for marker in PROCESS_MARKERS:
        if marker in name:
            return marker
    return None


def _strip_browser(title):
    return _BROWSER_SUFFIX.sub("", title).strip()


def _strip_unread(title):
    return _UNREAD.sub("", title).strip()


def _prepare_title(title):
    if title is None:
        return ""
    return _strip_unread(_strip_browser(str(title).strip()))


def _segments(text):
    """Split a Slack title on `` | `` and `` - ``, not on hyphens inside a name."""
    parts = re.split(r"\s*\|\s*|\s+[-–—]\s+", text)
    return [part.strip() for part in parts if part.strip()]


def _segment_is_huddle_label(part):
    """True when a segment is the Huddle chrome, not the conversation name.

    ``#huddle`` and ``Huddle-notes`` are names. ``Huddle`` and ``Slack Huddle``
    are the label Slack puts on the call window.
    """
    raw = re.sub(r"^🎧\s*", "", part).strip()
    lowered = raw.lower()
    if lowered in ("huddle", "slack huddle"):
        return True
    if re.match(r"^Huddle(?:\s|$)", raw, re.IGNORECASE):
        return True
    if re.match(r"^Slack\s+Huddle$", raw, re.IGNORECASE):
        return True
    return False


def _is_huddle_title(text):
    if not text:
        return False
    if _HEADPHONES_START.match(text):
        return True
    if _HUDDLE_START.match(text):
        return True
    for part in _segments(text):
        if _segment_is_huddle_label(part):
            return True
    return False


def browser_title_is_slack_huddle(title):
    """Extra check for a Huddle title that belongs to Chrome.

    ``slack.exe`` is already Slack. Chrome shares one microphone across every
    tab, so the title must end like the Slack app, be the bare Huddle popout,
    or be a short ``Huddle with / in / |`` title. Search pages and articles
    do not pass.
    """
    text = _prepare_title(title)
    if not text or not _is_huddle_title(text):
        return False
    if _SLACK_APP_SUFFIX.search(text):
        return True
    if _BARE_BROWSER_TITLE.match(text):
        return True
    if re.search(r"google search", text, re.IGNORECASE):
        return False
    if re.search(r"[.!?]", text):
        return False
    # "Huddle: #eng" is a call title. "Huddle with your team: a guide" is not.
    if re.search(r":\s+\S", text) and not re.match(r"^(?:🎧\s*)?Huddle\s*:", text, re.IGNORECASE):
        return False
    if not re.match(r"^(?:🎧\s*)?Huddle\b", text, re.IGNORECASE):
        return False
    words = re.findall(r"[A-Za-z0-9#@]+", text)
    return 1 < len(words) <= 10


def _conversation_name(text):
    """Channel or DM from a Huddle title, or None when the title has neither."""
    body = re.sub(r"^🎧\s*", "", text).strip()
    match = _WITH_OR_IN.match(body)
    if match:
        body = match.group(1).strip()
    else:
        body = _LEADING_HUDDLE.sub("", body, count=1).strip()
    body = _SLACK_APP_SUFFIX.sub("", body).strip()
    if "|" in body:
        parts = [part.strip() for part in body.split("|")]
    else:
        parts = [part.strip() for part in re.split(r"\s+[-–—]\s+", body)]
    cleaned = []
    for part in parts:
        part = re.sub(r"^🎧\s*", "", part).strip()
        if not part or part.lower() in ("huddle", "slack"):
            continue
        if _segment_is_huddle_label(part):
            continue
        cleaned.append(part)
    if not cleaned:
        return None
    # Slack titles are "<conversation> <separator> <workspace>".
    if len(cleaned) >= 2:
        cleaned = cleaned[:-1]
    name = cleaned[0].strip()
    if not name or name.lower() in ("huddle", "slack"):
        return None
    return name


def parse_huddle_window_title(title):
    """Return a Sighting when `title` is a Huddle window, else None.

    The display name is the channel or DM when the title includes one.
    Otherwise it is ``Slack Huddle`` and ``unnamed`` is set so the watcher
    can append the clock time. The key does not include that clock, so a
    title that gains a channel name during the join hold stays one call.
    """
    if title is None:
        return None
    raw = str(title).strip()
    if not raw:
        return None
    text = _prepare_title(raw)
    if not _is_huddle_title(text):
        return None
    name = _conversation_name(text)
    if name:
        slug = slugify(name) or name.lower()
        return Sighting(
            key="slack-huddle:" + slug,
            display_name=name,
            meet_code="",
            window_title=raw,
            source=SOURCE_SLACK_HUDDLE,
            unnamed=False,
        )
    return Sighting(
        key="slack-huddle",
        display_name=SLACK_HUDDLE_NAME,
        meet_code="",
        window_title=raw,
        source=SOURCE_SLACK_HUDDLE,
        unnamed=True,
    )


def _as_status(value):
    if isinstance(value, MicStatus):
        return value
    in_use = bool(value)
    return MicStatus(in_use, ("probe",) if in_use else ())


def _format_mic(marker, status):
    if status.registry or status.wasapi:
        detail = "registry=%s, wasapi=%s" % (
            status.registry or "no",
            status.wasapi or "no",
        )
    elif status.sources:
        detail = ", ".join(status.sources)
    else:
        detail = "no"
    state = "in use" if status.in_use else "not in use"
    return "Slack microphone %s (%s, %s)" % (state, marker, detail)


class SlackHuddleDetector:
    """Combine Huddle window titles with the microphone of that process.

    `windows` returns WindowInfo-like objects. `mic_in_use(marker)` returns a
    bool or a MicStatus for ``slack.exe`` or ``chrome.exe``. Either callable
    may be a fake in tests. A probe that raises counts as not detected.

    Joining requires a Huddle window and that process's microphone together.
    ``poll(in_call=True)`` keeps the call when either signal remains. A title
    that starts as ``Huddle`` and later gains a channel name keeps the same
    key. A different channel does not: the debouncer treats that as leaving.
    """

    source = SOURCE_SLACK_HUDDLE

    def __init__(self, windows, mic_in_use, log=None):
        self.windows = windows
        self.mic_in_use = mic_in_use
        self.log = log
        self._latched = None
        self._last_mic = _UNSEEN
        self._last_window = _UNSEEN
        self._last_notices = ()
        self._last_window_error = ""
        self._hold_reason = None
        self._saw_window = False
        self._saw_mic = False

    def poll(self, in_call=False):
        windows, window_error = self._read_windows()
        self._log_window_error(window_error)
        candidates = self._candidates(windows)
        mics = {
            marker: self._read_mic(marker)
            for marker in self._markers_to_probe(candidates, bool(in_call))
        }
        self._log_mics(mics)
        sighting = self._decide(candidates, mics, bool(in_call))
        shown = self._ranked(candidates)[0][1] if candidates else None
        self._log_window(shown)
        self._log_hold(sighting)
        return sighting

    def _read_windows(self):
        try:
            return list(self.windows() or []), None
        except Exception as exc:
            return [], exc

    def _candidates(self, windows):
        found = []
        for window in windows:
            marker = marker_for_process(getattr(window, "process_name", ""))
            if marker is None:
                continue
            title = getattr(window, "title", "")
            if marker == BROWSER_MARKER and not browser_title_is_slack_huddle(title):
                continue
            parsed = parse_huddle_window_title(title)
            if parsed is None:
                continue
            found.append((marker, parsed))
        return found

    def _markers_to_probe(self, candidates, in_call):
        markers = []
        for marker, _sighting in candidates:
            if marker not in markers:
                markers.append(marker)
        if in_call and self._latched is not None:
            latched_marker = self._latched[0]
            if latched_marker not in markers:
                markers.append(latched_marker)
        return markers

    def _read_mic(self, marker):
        try:
            return _as_status(self.mic_in_use(marker))
        except Exception as exc:
            return MicStatus(
                False,
                (),
                notice="Slack microphone probe failed for %s (%s)" % (marker, exc),
            )

    def _decide(self, candidates, mics, in_call):
        self._saw_window = False
        self._saw_mic = False
        live = self._first_with_mic(candidates, mics)
        if live is not None:
            marker, sighting = live
            self._saw_window = True
            self._saw_mic = True
            if in_call and self._latched is not None:
                same = self._as_same_call(sighting)
                if same is None:
                    # A different Huddle while the mic is live. The debouncer
                    # sees the new key and runs the leave hold before joining.
                    self._latched = (marker, sighting)
                    return sighting
                sighting = same
            self._latched = (marker, sighting)
            return sighting
        if not in_call or self._latched is None:
            return None
        for marker, sighting in self._ranked(candidates):
            same = self._as_same_call(sighting)
            if same is None:
                continue
            self._saw_window = True
            self._latched = (marker, same)
            return same
        marker, latched = self._latched
        status = mics.get(marker)
        if status is not None and status.in_use:
            self._saw_mic = True
            return latched
        return None

    def _first_with_mic(self, candidates, mics):
        for marker, sighting in self._ranked(candidates):
            status = mics.get(marker)
            if status is not None and status.in_use:
                return marker, sighting
        return None

    def _ranked(self, candidates):
        """Desktop before Chrome, a named conversation before a bare Huddle."""
        decorated = []
        for index, (marker, sighting) in enumerate(candidates):
            desktop = 0 if marker == DESKTOP_MARKER else 1
            unnamed = 1 if sighting.unnamed else 0
            decorated.append((desktop, unnamed, index, marker, sighting))
        decorated.sort()
        return [(marker, sighting) for _desktop, _unnamed, _index, marker, sighting in decorated]

    def _as_same_call(self, sighting):
        """Keep the latched key when this window is the same Huddle.

        A bare ``Huddle`` title that later gains a channel is the same call.
        A different channel is not.
        """
        if self._latched is None:
            return sighting
        _marker, latched = self._latched
        if sighting.key == latched.key:
            return Sighting(
                key=latched.key,
                display_name=latched.display_name,
                meet_code="",
                window_title=sighting.window_title,
                source=SOURCE_SLACK_HUDDLE,
                unnamed=latched.unnamed,
            )
        if latched.unnamed and not sighting.unnamed:
            return Sighting(
                key=latched.key,
                display_name=sighting.display_name,
                meet_code="",
                window_title=sighting.window_title,
                source=SOURCE_SLACK_HUDDLE,
                unnamed=False,
            )
        return None

    def _log_mics(self, mics):
        current = tuple(
            (marker, status.in_use, status.registry, status.wasapi, status.sources)
            for marker, status in sorted(mics.items())
        )
        if current != self._last_mic:
            self._last_mic = current
            for marker, status in sorted(mics.items()):
                self._info(_format_mic(marker, status))
        notices = tuple(
            status.notice for _marker, status in sorted(mics.items()) if status.notice
        )
        if notices != self._last_notices:
            self._last_notices = notices
            for notice in notices:
                self._warn(notice)

    def _log_window_error(self, window_error):
        error = "" if window_error is None else "Slack window probe failed (%s)" % window_error
        if error != self._last_window_error:
            self._last_window_error = error
            if error:
                self._warn(error)

    def _log_window(self, sighting):
        current = None if sighting is None else (sighting.key, sighting.window_title)
        if current == self._last_window:
            return
        self._last_window = current
        if sighting is None:
            self._info("Slack Huddle window not detected")
        else:
            self._info("Slack Huddle window detected: %s", sighting.window_title)

    def _log_hold(self, sighting):
        reason = None
        if sighting is not None and not (self._saw_window and self._saw_mic):
            if self._saw_window and not self._saw_mic:
                reason = "window"
            elif self._saw_mic and not self._saw_window:
                reason = "mic"
        if reason == self._hold_reason:
            return
        self._hold_reason = reason
        if reason == "window":
            self._info(
                "Holding the Huddle because its window is still open; "
                "the microphone is not in use."
            )
        elif reason == "mic":
            self._info(
                "Holding the Huddle because the microphone is still in use; "
                "the Huddle window is not visible."
            )

    def _info(self, msg, *args):
        if self.log is not None:
            self.log.info(msg, *args)

    def _warn(self, msg, *args):
        if self.log is not None:
            self.log.warning(msg, *args)
