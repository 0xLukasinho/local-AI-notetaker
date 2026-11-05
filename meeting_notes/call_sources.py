"""Which calls the watcher is willing to record.

Each source is one detector module. ``source_factories`` is the list. Adding
Zoom or Teams means a new module, a config flag, and one row in that list.
The composite polls them. Only one call is active at a time:

* The source that is already in progress keeps the session. A Huddle that
  starts during a Meet (or the reverse) is logged and ignored until that
  call's own leave condition has cleared.
* When nothing is in progress and more than one source matches on the same
  sample, the earlier row wins. Google Meet is first, then Slack Huddle.
  The other call can start only after this one ends.
"""

from meeting_notes.meet_detect import ChromeMeetDetector
from meeting_notes.slack_detect import SOURCE_SLACK_HUDDLE, SlackHuddleDetector


SOURCE_GOOGLE_MEET = "google-meet"


def _meet_detector(config, windows, probe_microphone, log):
    marker = config.mic_process_marker or "chrome.exe"
    detector = ChromeMeetDetector(
        windows=windows,
        mic_in_use=lambda: probe_microphone(marker),
        title_regex=config.window_title_regex or None,
        process_marker=marker,
        log=log,
    )
    detector.source = SOURCE_GOOGLE_MEET
    return detector


def _slack_detector(_config, windows, probe_microphone, log):
    return SlackHuddleDetector(
        windows=windows,
        mic_in_use=probe_microphone,
        log=log,
    )


def source_factories():
    """Enabled-flag name and builder, in the order a new call is chosen.

    A builder is ``(config, windows, probe_microphone, log) -> detector``.
    The detector's ``poll(in_call)`` returns a Sighting or None. Sighting.source
    is the id used in status.json and the webhook payload.
    """
    return (
        (SOURCE_GOOGLE_MEET, "watch_google_meet", _meet_detector),
        (SOURCE_SLACK_HUDDLE, "watch_slack_huddle", _slack_detector),
    )


def build_call_detector(config, windows, probe_microphone, log=None):
    """The watcher detector: every enabled source, or a no-op when none are."""
    detectors = []
    for source_id, flag, build in source_factories():
        if not getattr(config, flag, True):
            continue
        detector = build(config, windows, probe_microphone, log)
        detector.source = source_id
        detectors.append(detector)
    return CompositeCallDetector(detectors, log=log)


class CompositeCallDetector:
    """Poll several source detectors as one call signal.

    ``poll(in_call=True)`` is passed through only to the source that already
    owns the call, so a mute or a background tab keeps using that source's
    hold rules. Other sources are observed so the log can say they were
    ignored. They do not take over until ``poll(in_call=False)``, which is
    after the debouncer has finished the leave hold.
    """

    def __init__(self, detectors, log=None):
        self.detectors = list(detectors)
        self.log = log
        self._owner = None
        self._overlap_token = None

    def poll(self, in_call=False):
        if not in_call:
            self._owner = None
        if self._owner:
            sighting = self._poll_owner(self._owner, in_call=True)
            if sighting is not None:
                self._note_others(self._owner)
                return sighting
            self._owner = None
            if in_call:
                return None
        found = self._scan()
        if not found:
            self._overlap_token = None
            return None
        chosen = found[0]
        self._owner = chosen.source
        if len(found) > 1:
            self._log_overlap(chosen, found[1:], during=False)
        return chosen

    def _scan(self):
        found = []
        for detector in self.detectors:
            sighting = self._poll(detector, in_call=False)
            if sighting is not None:
                found.append(sighting)
        return found

    def _poll_owner(self, source, in_call):
        for detector in self.detectors:
            if getattr(detector, "source", None) == source:
                return self._poll(detector, in_call=in_call)
        return None

    def _note_others(self, owner):
        """Log other sources that are also visible. Do not poll the owner again."""
        others = []
        for detector in self.detectors:
            if getattr(detector, "source", None) == owner:
                continue
            sighting = self._poll(detector, in_call=False)
            if sighting is not None:
                others.append(sighting)
        if not others:
            self._overlap_token = None
            return
        self._log_overlap_sources(owner, [item.source for item in others], during=True)

    def _poll(self, detector, in_call):
        try:
            return detector.poll(in_call=in_call)
        except Exception as exc:
            self._warn(
                "Call detector %s failed (%s)",
                getattr(detector, "source", detector),
                exc,
            )
            return None

    def _log_overlap(self, chosen, others, during):
        self._log_overlap_sources(chosen.source, [item.source for item in others], during)

    def _log_overlap_sources(self, chosen_source, other_sources, during):
        names = tuple(other_sources)
        token = (chosen_source, names, during)
        if token == self._overlap_token:
            return
        self._overlap_token = token
        listed = ", ".join(names)
        if during:
            self._info(
                "Also seeing %s. Staying with %s until that call ends.",
                listed,
                chosen_source,
            )
            return
        self._info(
            "Seeing %s and %s at the same time. Recording %s. "
            "The other call is recorded only after this one ends.",
            chosen_source,
            listed,
            chosen_source,
        )

    def _info(self, msg, *args):
        if self.log is not None:
            self.log.info(msg, *args)

    def _warn(self, msg, *args):
        if self.log is not None:
            self.log.warning(msg, *args)
