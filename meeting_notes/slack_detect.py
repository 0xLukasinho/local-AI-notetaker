"""Slack Huddle detection that does not touch Windows APIs.

The desktop app (``slack.exe``) is in a Huddle when it is holding the
microphone. The watcher's join debounce (default 12 seconds) is what ignores
a short voice clip or a mic test. The leave debounce (default 25 seconds)
ends the session after ``slack.exe`` releases the microphone. A window that
stays open does not keep the recording going.

Window titles are optional names. When a ``slack.exe`` title clearly has a
channel or DM, that name is used. Otherwise the session is "Slack Huddle"
plus the clock time. A title is never required to start or stop.

Slack in Chrome is not detected. ``chrome.exe`` shares one microphone across
every tab, Slack's title does not say that a Huddle is in progress, and a
Slack tab is often left open while Chrome's mic is in use for something
else. Google Meet keeps priority for ``chrome.exe``.
"""

import re

from meeting_notes.meet_detect import MicStatus, Sighting


SOURCE_SLACK_HUDDLE = "slack-huddle"
SLACK_HUDDLE_NAME = "Slack Huddle"
DESKTOP_MARKER = "slack.exe"

_BROWSER_SUFFIX = re.compile(
    r"\s+[-–—]\s+(?:Google Chrome|Mozilla Firefox|Microsoft Edge)(?:\s.*)?$",
    re.IGNORECASE,
)
_UNREAD = re.compile(r"^(?:\(\d+\)\s*)+")
_SLACK_APP_SUFFIX = re.compile(r"\s*(?:\||[-–—])\s*Slack\s*$", re.IGNORECASE)
_CHANNEL = re.compile(r"#([A-Za-z0-9][\w-]*)")
_ROLE = re.compile(
    r"\s*\((?:Channel|DM|Group DM|Direct Message)\)\s*$",
    re.IGNORECASE,
)
# Slack views that show up as the first title segment and are not a conversation.
_GENERIC = {
    "slack",
    "huddle",
    "preferences",
    "settings",
    "activity",
    "later",
    "drafts",
    "drafts & sent",
    "home",
    "threads",
    "search",
    "unreads",
    "mentions",
    "more unreads",
    "directory",
    "dms",
    "all dms",
    "saved",
    "new message",
    "browse channels",
}

_UNSEEN = object()


def _strip_browser(title):
    return _BROWSER_SUFFIX.sub("", title).strip()


def _strip_unread(title):
    return _UNREAD.sub("", title).strip()


def _prepare_title(title):
    if title is None:
        return ""
    text = str(title).strip()
    if not text:
        return ""
    text = _strip_unread(_strip_browser(text))
    return re.sub(r"^🎧\s*", "", text).strip()


def _segments(text):
    parts = re.split(r"\s*\|\s*|\s+[-–—]\s+", text)
    return [part.strip() for part in parts if part.strip()]


def _clean_segment(part):
    return _ROLE.sub("", part).strip()


def conversation_from_slack_title(title):
    """Channel or DM from a Slack window title, or None when it is not clear.

    Clear means a ``#channel``, a ``(Channel)`` / ``(DM)`` / ``(Group DM)``
    label, or Slack's usual ``conversation - workspace - Slack`` title whose
    first segment is not a built-in view (Activity, Preferences, and so on).
    ``Slack`` alone and ``Workspace - Slack`` are not a conversation.
    """
    text = _prepare_title(title)
    if not text:
        return None
    channel = _CHANNEL.search(text)
    if channel:
        return "#" + channel.group(1)
    for part in _segments(text):
        if _ROLE.search(part):
            name = _clean_segment(part)
            if name and name.lower() not in _GENERIC:
                return name
    if _SLACK_APP_SUFFIX.search(text) is None:
        return None
    body = _SLACK_APP_SUFFIX.sub("", text).strip()
    cleaned = []
    for part in _segments(body):
        part = _clean_segment(part)
        if part and part.lower() != "slack":
            cleaned.append(part)
    if len(cleaned) < 2:
        return None
    name = cleaned[0]
    if name.lower() in _GENERIC:
        return None
    if len(name) > 80 or re.search(r"[.!?]", name):
        return None
    return name


def _as_status(value):
    if isinstance(value, MicStatus):
        return value
    in_use = bool(value)
    return MicStatus(in_use, ("probe",) if in_use else ())


def _format_mic(status):
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
    return "Slack microphone %s (%s, %s)" % (state, DESKTOP_MARKER, detail)


def _best_name(windows):
    """Best (display name, window title) from slack.exe windows, or (None, "")."""
    ranked = []
    for index, window in enumerate(windows or []):
        process_name = (getattr(window, "process_name", "") or "").lower()
        if DESKTOP_MARKER not in process_name:
            continue
        title = getattr(window, "title", "") or ""
        name = conversation_from_slack_title(title)
        if not name:
            continue
        rank = 0 if name.startswith("#") else 1
        ranked.append((rank, index, name, title))
    if not ranked:
        return None, ""
    ranked.sort()
    _rank, _index, name, title = ranked[0]
    return name, title


def _sighting(name, window_title):
    if name:
        return Sighting(
            key="slack-huddle",
            display_name=name,
            meet_code="",
            window_title=window_title,
            source=SOURCE_SLACK_HUDDLE,
            unnamed=False,
        )
    return Sighting(
        key="slack-huddle",
        display_name=SLACK_HUDDLE_NAME,
        meet_code="",
        window_title=window_title,
        source=SOURCE_SLACK_HUDDLE,
        unnamed=True,
    )


class SlackHuddleDetector:
    """Report a Huddle for as long as ``slack.exe`` holds the microphone.

    `windows` returns WindowInfo-like objects and is used only to name the
    session. `mic_in_use(marker)` returns a bool or a MicStatus. Either
    callable may be a fake in tests. A probe that raises counts as not in use.

    ``poll(in_call=...)`` does not latch on a window. Mic released means the
    signal is absent, including while a call is already recording, so the
    watcher's leave debounce can end the session.
    """

    source = SOURCE_SLACK_HUDDLE

    def __init__(self, windows, mic_in_use, log=None):
        self.windows = windows
        self.mic_in_use = mic_in_use
        self.log = log
        self._remembered = None
        self._last_mic = _UNSEEN
        self._last_notice = ""
        self._last_window_error = ""
        self._last_name = _UNSEEN

    def poll(self, in_call=False):
        # `in_call` is part of the detector interface. This source does not
        # use it to keep recording after the microphone is released.
        del in_call
        status = self._read_mic()
        self._log_mic(status)
        if not status.in_use:
            self._remembered = None
            self._log_name(None)
            return None
        windows, window_error = self._read_windows()
        self._log_window_error(window_error)
        name, title = _best_name(windows)
        if name is None and self._remembered is not None:
            name = self._remembered[0]
            title = title or self._remembered[1]
        sighting = _sighting(name, title)
        if name:
            self._remembered = (name, title)
        self._log_name(sighting)
        return sighting

    def _read_mic(self):
        try:
            return _as_status(self.mic_in_use(DESKTOP_MARKER))
        except Exception as exc:
            return MicStatus(
                False,
                (),
                notice="Slack microphone probe failed for %s (%s)" % (DESKTOP_MARKER, exc),
            )

    def _read_windows(self):
        try:
            return list(self.windows() or []), None
        except Exception as exc:
            return [], exc

    def _log_mic(self, status):
        current = (status.in_use, status.registry, status.wasapi, status.sources)
        if current != self._last_mic:
            self._last_mic = current
            self._info(_format_mic(status))
        if status.notice != self._last_notice:
            self._last_notice = status.notice
            if status.notice:
                self._warn(status.notice)

    def _log_window_error(self, window_error):
        error = "" if window_error is None else "Slack window probe failed (%s)" % window_error
        if error != self._last_window_error:
            self._last_window_error = error
            if error:
                self._warn(error)

    def _log_name(self, sighting):
        current = None if sighting is None else (sighting.unnamed, sighting.display_name)
        if current == self._last_name:
            return
        self._last_name = current
        if sighting is None:
            return
        if sighting.unnamed:
            self._info(
                "Slack is using the microphone. No channel or DM in the window title."
            )
        else:
            self._info(
                "Slack is using the microphone. Session name: %s",
                sighting.display_name,
            )

    def _info(self, msg, *args):
        if self.log is not None:
            self.log.info(msg, *args)

    def _warn(self, msg, *args):
        if self.log is not None:
            self.log.warning(msg, *args)
