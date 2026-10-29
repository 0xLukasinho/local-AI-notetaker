"""Google Meet detection primitives that do not touch Windows APIs.

A call *starts* only when both signals agree: a top-level Chrome window
titled like ``Meet - <code or title>`` AND the microphone in use by the
browser process. The lobby page does not match that title.

Once the call is active, either signal keeps it. Muting in Meet can release
the capture session for longer than the leave debounce, and Chrome's window
title is only the active tab, so requiring both signals for the whole call
ends real meetings. See ``ChromeMeetDetector.poll``.

CallDebouncer turns that signal into join/leave events with separate hold
times so a reload or a brief blip does not split one call into two sessions.
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


# IAudioSessionControl::GetState. Inactive (0) and Expired (2) are not
# "in use". A mute often stops the capture client; the in-call window hold
# covers that. Treating Inactive as live would also keep a stale session
# after the call has actually released the microphone.
AUDIO_SESSION_STATE_ACTIVE = 1

_UNSEEN = object()


@dataclass(frozen=True)
class MicStatus:
    """Result of the registry probe OR-ed with the WASAPI probe.

    `registry` and `wasapi` are ``yes``, ``no``, or ``unavailable``.
    Empty strings mean the caller used the legacy bool mic probe.
    `sources` lists the probes that said yes, in stable order.
    `notice` is a one-shot operator message (missing dependency, probe error).
    """

    in_use: bool
    sources: tuple = ()
    registry: str = ""
    wasapi: str = ""
    notice: str = ""


def make_mic_status(registry, wasapi, notice=""):
    """Build a MicStatus from the two probe outcomes."""
    sources = []
    if registry == "yes":
        sources.append("registry")
    if wasapi == "yes":
        sources.append("wasapi")
    return MicStatus(
        in_use=bool(sources),
        sources=tuple(sources),
        registry=registry,
        wasapi=wasapi,
        notice=notice,
    )


def mic_in_use_from_capture_sessions(sessions, marker="chrome.exe"):
    """True when an active WASAPI capture session belongs to `marker`.

    `sessions` is a sequence of ``(process_name, state)``. State ``1`` is
    ``AudioSessionStateActive``.
    """
    needle = (marker or "").lower()
    if not needle:
        return False
    for item in sessions or []:
        name, state = item[0], item[1]
        if needle not in str(name).lower():
            continue
        try:
            if int(state) == AUDIO_SESSION_STATE_ACTIVE:
                return True
        except (TypeError, ValueError):
            continue
    return False


def combine_mic_signals(
    marker,
    registry_entries,
    capture_sessions,
    registry_available=True,
    wasapi_available=True,
    notice="",
):
    """OR the consent-store and WASAPI results.

    A probe that did not run (`*_available` false) is ``unavailable`` and
    does not count as in use. An available probe with no matching entry is
    ``no``. Either ``yes`` is enough.
    """
    if not registry_available:
        registry = "unavailable"
    elif mic_in_use_from_entries(registry_entries or [], marker):
        registry = "yes"
    else:
        registry = "no"
    if not wasapi_available:
        wasapi = "unavailable"
    elif mic_in_use_from_capture_sessions(capture_sessions or [], marker):
        wasapi = "yes"
    else:
        wasapi = "no"
    return make_mic_status(registry, wasapi, notice=notice)


def _coerce_mic(value):
    """Accept a MicStatus or the legacy bool from tests and older callers."""
    if isinstance(value, MicStatus):
        return value
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], bool)
        and not isinstance(value[1], str)
    ):
        in_use, sources = value
        return MicStatus(bool(in_use), tuple(sources))
    in_use = bool(value)
    return MicStatus(in_use, ("probe",) if in_use else ())


class ChromeMeetDetector:
    """Combine a window list and a mic probe.

    Joining requires both: a Meet-titled window of `process_marker` and the
    microphone in use. `mic_in_use` may return a bool or a MicStatus. Either
    callable may be a fake in tests. A probe that raises counts as "not
    detected" so a flaky read cannot start a recording.

    `poll(in_call=True)` is used while a join is arming and for the rest of
    the call. The first sample still requires the Meet window and the mic
    together. After that, mute can drop the capture session for longer than
    the leave debounce, so the Meet window alone keeps the call. Chrome only
    puts the active tab in the window title, so the microphone alone keeps
    the same call when the Meet tab is in the background. The call ends when
    both have been absent long enough for the leave debounce. A Meet tab left
    open on the post-call page is indistinguishable from a mute; switch away
    from it (or close it) after leaving so the watcher can stop. When
    `in_call` is false a stale latch is ignored, so a later Chrome mic use
    does not resurrect the meeting.
    """

    def __init__(
        self,
        windows,
        mic_in_use,
        title_regex=None,
        process_marker="chrome.exe",
        log=None,
    ):
        self.windows = windows
        self.mic_in_use = mic_in_use
        self.title_regex = title_regex or None
        self.process_marker = process_marker or "chrome.exe"
        self.log = log
        self._latched = None
        self._last_mic = _UNSEEN
        self._last_window = _UNSEEN
        self._last_notice = ""
        self._last_window_error = ""
        self._hold_reason = None

    def poll(self, in_call=False):
        mic = self._read_mic()
        window, window_error = self._read_window()
        self._log_mic(mic)
        self._log_window(window, window_error)
        sighting = self._decide(mic, window, bool(in_call))
        self._log_hold(mic, window, sighting)
        return sighting

    def _read_mic(self):
        try:
            return _coerce_mic(self.mic_in_use())
        except Exception as exc:
            return MicStatus(
                False,
                (),
                notice="Microphone probe failed (%s)" % exc,
            )

    def _read_window(self):
        try:
            windows = self.windows() or []
        except Exception as exc:
            return None, exc
        marker = self.process_marker.lower()
        for window in windows:
            process_name = (getattr(window, "process_name", "") or "").lower()
            if marker not in process_name:
                continue
            parsed = parse_meet_window_title(
                getattr(window, "title", ""),
                self.title_regex,
            )
            if parsed is not None:
                return parsed, None
        return None, None

    def _decide(self, mic, window, in_call):
        if window is not None and mic.in_use:
            self._latched = window
            return window
        if not in_call or self._latched is None:
            return None
        if window is not None and window.key == self._latched.key:
            self._latched = window
            return window
        if mic.in_use:
            return self._latched
        return None

    def _log_mic(self, mic):
        current = (mic.in_use, mic.registry, mic.wasapi, mic.sources)
        if current != self._last_mic:
            self._last_mic = current
            self._info(_format_mic(mic))
        if mic.notice != self._last_notice:
            self._last_notice = mic.notice
            if mic.notice:
                self._warn(mic.notice)

    def _log_window(self, window, window_error):
        error = "" if window_error is None else "Meet window probe failed (%s)" % window_error
        if error != self._last_window_error:
            self._last_window_error = error
            if error:
                self._warn(error)
        current = None if window is None else (window.key, window.window_title)
        if current == self._last_window:
            return
        self._last_window = current
        if window is None:
            self._info("Meet window not detected")
        else:
            self._info("Meet window detected: %s", window.window_title)

    def _log_hold(self, mic, window, sighting):
        reason = None
        if sighting is not None and not (window is not None and mic.in_use):
            if window is not None and not mic.in_use:
                reason = "window"
            elif mic.in_use and window is None:
                reason = "mic"
        if reason == self._hold_reason:
            return
        self._hold_reason = reason
        if reason == "window":
            self._info(
                "Holding the call because the Meet window is still open; "
                "the microphone is not in use."
            )
        elif reason == "mic":
            self._info(
                "Holding the call because the microphone is still in use; "
                "the Meet tab is not the active Chrome window."
            )

    def _info(self, msg, *args):
        if self.log is not None:
            self.log.info(msg, *args)

    def _warn(self, msg, *args):
        if self.log is not None:
            self.log.warning(msg, *args)


def _format_mic(status):
    if status.registry or status.wasapi:
        detail = "registry=%s, wasapi=%s" % (
            status.registry or "no",
            status.wasapi or "no",
        )
        if status.in_use:
            return "Microphone in use (%s)" % detail
        return "Microphone not in use (%s)" % detail
    if status.in_use:
        extra = ", ".join(status.sources)
        if extra:
            return "Microphone in use (%s)" % extra
        return "Microphone in use"
    return "Microphone not in use"


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
