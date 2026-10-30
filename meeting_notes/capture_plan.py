"""Pure planning for Windows capture routes.

The recorder opens a WASAPI loopback per playback endpoint and keeps those
streams aligned to the microphone clock. Device names and silence decisions
live here so they can be tested without PortAudio.
"""


# A remote participant who is only quiet for a few seconds should not trip
# this. A full minute of microphone audio with no system audio is long enough
# to mean the loopback is pointed at a dead endpoint (or the other side is
# genuinely silent — the log says which devices were open).
SYSTEM_SILENCE_WARN_SECONDS = 60.0
SYSTEM_SILENCE_REMIND_SECONDS = 300.0
# RMS of float32 samples in [-1, 1]. Speech on a laptop mic is usually well
# above the noise floor at 0.02; loopback of a remote voice is quieter, so the
# system-audio bar is lower.
MIC_ACTIVE_RMS = 0.02
SPEAKER_ACTIVE_RMS = 0.005
# A device must be missing for two scans before its stream is closed, so one
# flaky enumeration during a headset switch does not drop a live loopback.
OUTPUT_MISSES_BEFORE_STOP = 2


class OutputEndpoint:
    """One render endpoint's loopback, as PortAudio reports it."""

    def __init__(self, name, is_default=False):
        self.name = name
        self.is_default = bool(is_default)

    def __eq__(self, other):
        return (
            isinstance(other, OutputEndpoint)
            and self.name == other.name
            and self.is_default == other.is_default
        )

    def __repr__(self):
        return f"OutputEndpoint({self.name!r}, is_default={self.is_default})"


class AudioRoute:
    """Microphone name plus every loopback endpoint currently enumerated."""

    def __init__(self, microphone, outputs):
        self.microphone = microphone
        self.outputs = tuple(outputs)

    def __eq__(self, other):
        return (
            isinstance(other, AudioRoute)
            and self.microphone == other.microphone
            and self.outputs == other.outputs
        )


def snapshot_route(microphone, outputs):
    """Copy a route with outputs ordered by name and duplicate names collapsed."""
    if microphone is not None:
        microphone = str(microphone).strip() or None
    unique = {}
    for endpoint in outputs:
        unique[endpoint.name] = OutputEndpoint(endpoint.name, endpoint.is_default)
    ordered = tuple(unique[name] for name in sorted(unique))
    return AudioRoute(microphone, ordered)


def mark_default_loopback(render_name, loopback_names):
    """Return the loopback name that belongs to the default render endpoint.

    PyAudioWPatch names the loopback ``"<render name> [Loopback]"`` and matches
    by substring. Prefer the exact ``[Loopback]`` suffix, then the shortest
    containing name, so ``Speakers`` does not steal ``Speakers (HD) [Loopback]``
    when the exact device is also present.
    """
    names = [name for name in loopback_names if name]
    if not render_name:
        return None
    exact = f"{render_name} [Loopback]"
    if exact in names:
        return exact
    if render_name in names:
        return render_name
    containing = [name for name in names if render_name in name]
    if not containing:
        return None
    containing.sort(key=lambda name: (len(name), name))
    return containing[0]


def describe_route_change(previous, current):
    """Lines to log when the captured inputs or outputs change.

    ``previous is None`` is the first observation and lists every device.
    Later calls report additions, removals, microphone changes, and default
    output changes. A device that stays open is not mentioned again.
    """
    if current is None:
        return []
    if previous is None:
        lines = []
        if current.microphone:
            lines.append(f"capturing microphone '{current.microphone}'")
        else:
            lines.append("no microphone selected")
        if not current.outputs:
            lines.append("no output loopback selected")
        for endpoint in current.outputs:
            role = "default output" if endpoint.is_default else "non-default output"
            lines.append(f"capturing {role} '{endpoint.name}'")
        return lines

    lines = []
    if previous.microphone != current.microphone:
        lines.append(
            f"microphone switched: '{previous.microphone}' -> '{current.microphone}'"
        )
    previous_names = {endpoint.name for endpoint in previous.outputs}
    current_names = {endpoint.name for endpoint in current.outputs}
    for endpoint in current.outputs:
        if endpoint.name in previous_names:
            continue
        role = "default output" if endpoint.is_default else "non-default output"
        lines.append(f"output added: '{endpoint.name}' ({role})")
    for endpoint in previous.outputs:
        if endpoint.name not in current_names:
            lines.append(f"output removed: '{endpoint.name}'")
    previous_default = _default_name(previous)
    current_default = _default_name(current)
    if previous_default != current_default:
        lines.append(
            f"default output switched: '{previous_default}' -> '{current_default}'"
        )
    return lines


def _default_name(route):
    for endpoint in route.outputs:
        if endpoint.is_default:
            return endpoint.name
    return None


def note_output_misses(misses, open_names, wanted_names, misses_before_stop=OUTPUT_MISSES_BEFORE_STOP):
    """Count scans that omit an open loopback.

    Returns ``(still_missing, stop_names)``. A device that is wanted again is
    dropped from the miss map. ``stop_names`` are devices that have been absent
    for ``misses_before_stop`` scans and should be closed.
    """
    wanted = set(wanted_names)
    still_missing = {}
    stop_names = []
    for name in open_names:
        if name in wanted:
            continue
        count = int(misses.get(name, 0)) + 1
        if count >= misses_before_stop:
            stop_names.append(name)
        else:
            still_missing[name] = count
    return still_missing, stop_names


def outputs_to_open(open_names, wanted_names):
    """Loopback names that should gain a capture thread."""
    open_set = set(open_names)
    return [name for name in wanted_names if name not in open_set]


def take_span(available, needed):
    """How many samples to consume from a device buffer, and how many to pad.

    Short buffers are padded for this block. They are not delayed to the next
    block, so a quiet endpoint cannot shift a louder one's audio later.
    """
    if needed <= 0:
        return 0, 0
    if available <= 0:
        return 0, needed
    take = available if available < needed else needed
    return take, needed - take


def sum_aligned(channels, needed):
    """Sum per-device sample lists into one block of ``needed`` floats.

    Each channel contributes its leading samples and keeps the tail. Missing
    samples are silence for this block. Returns ``(mixed, remainders)``.
    """
    mixed = [0.0] * max(0, needed)
    remainders = []
    for channel in channels:
        take, _pad = take_span(len(channel), needed)
        for index in range(take):
            mixed[index] += channel[index]
        remainders.append(list(channel[take:]))
    return mixed, remainders


def rms_from_sum_squares(sum_squares, count):
    if count <= 0 or sum_squares <= 0:
        return 0.0
    return (float(sum_squares) / float(count)) ** 0.5


class SilenceTracker:
    """Warn when the mic is active and system audio is not.

    Time advances only on windows where the microphone is above the speech
    threshold, so a pause in the user's own voice does not reset the clock.
    Any window with audible system audio clears the streak.
    """

    def __init__(
        self,
        warn_after_s=SYSTEM_SILENCE_WARN_SECONDS,
        remind_every_s=SYSTEM_SILENCE_REMIND_SECONDS,
        mic_threshold=MIC_ACTIVE_RMS,
        speaker_threshold=SPEAKER_ACTIVE_RMS,
    ):
        self.warn_after_s = float(warn_after_s)
        self.remind_every_s = float(remind_every_s)
        self.mic_threshold = float(mic_threshold)
        self.speaker_threshold = float(speaker_threshold)
        self._elapsed = 0.0
        self._warned = False
        self._notice_at = 0.0

    def observe_window(self, mic_rms, speaker_rms, window_s=1.0):
        """Return ``warn``, ``remind``, ``recovered``, or None for this window."""
        if speaker_rms >= self.speaker_threshold:
            warned = self._warned
            self._elapsed = 0.0
            self._warned = False
            self._notice_at = 0.0
            return "recovered" if warned else None
        if mic_rms < self.mic_threshold:
            return None
        self._elapsed += float(window_s)
        if not self._warned and self._elapsed >= self.warn_after_s:
            self._warned = True
            self._notice_at = self._elapsed
            return "warn"
        if self._warned and (self._elapsed - self._notice_at) >= self.remind_every_s:
            self._notice_at = self._elapsed
            return "remind"
        return None
