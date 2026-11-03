"""Attribute transcript lines to people.

The microphone track is the local user (`self_name`). System-audio loopback is
everyone else. Google Meet display names, when the Chrome extension posted
them during the call, are aligned onto the remote track by time. Without
those names the remote track is "Them". Stable anonymous ids become
"Speaker A", "Speaker B", and so on.

This module does not open devices or load Whisper. Callers pass transcript
segments and speaker samples in.
"""

import array
import json
import os
import sys
import wave
from difflib import SequenceMatcher


THEM = "Them"
TRANSCRIPT_JSON = "transcript.json"
SPEAKER_LOG = "speakers.jsonl"
SELF_TRACK = "audio-self.wav"
OTHERS_TRACK = "audio-others.wav"

# A Meet sample covers until the next sample, but not longer than this. The
# extension heartbeats about once a second while a call is open.
SAMPLE_COVER_SECONDS = 1.6
TURN_GAP_SECONDS = 1.0
# Skip Whisper on a track this quiet. Speech sits well above it; a muted or
# unused track does not, and Whisper will invent words on digital silence.
SILENT_TRACK_RMS = 0.004
BLEED_OVERLAP = 0.55
BLEED_SIMILARITY = 0.84

SPEAKER_LIMITS = [
    "The microphone track is labeled with self_name. Other people in the same room share that label.",
    "Remote audio is one system-loopback mix. When two people talk at once, the line is named for whoever Meet showed as the active speaker, or both names when neither dominates.",
    "A conference-room speakerphone is a single Meet name, not each person around it.",
    "Names are read from the Meet tab while it is open. A background tab, no Chrome extension, or a Meet UI change falls back to Them or Speaker A/B.",
    "Loudspeakers can leak remote voices into the microphone. Matching wording is kept on the named remote line. Headphones avoid that leak.",
]


def clean_name(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def clean_text(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def track_rms(path):
    """RMS of a 16-bit PCM WAV, or None when the file is not that format."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2 or handle.getnchannels() < 1:
            return None
        channels = handle.getnchannels()
        total = 0.0
        count = 0
        while True:
            raw = handle.readframes(16000)
            if not raw:
                break
            usable = raw[: len(raw) - (len(raw) % 2)]
            if not usable:
                continue
            samples = array.array("h")
            samples.frombytes(usable)
            if sys.byteorder != "little":
                samples.byteswap()
            if channels > 1:
                samples = samples[::channels]
            for value in samples:
                total += float(value) * float(value)
                count += 1
        if count == 0:
            return 0.0
        return (total / count) ** 0.5 / 32768.0


def track_has_speech(path, threshold=SILENT_TRACK_RMS):
    """False only when the file is a readable WAV below `threshold`.

    An unreadable file returns True so the caller still tries transcription.
    """
    try:
        rms = track_rms(path)
    except (OSError, wave.Error, EOFError):
        return True
    if rms is None:
        return True
    return rms >= threshold


def _unique(items):
    seen = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def coerce_event(item):
    """Normalize one speaker sample. Returns None when it has no timestamp."""
    if not isinstance(item, dict) or item.get("kind") == "start":
        return None
    if "t" not in item:
        return None
    try:
        moment = float(item["t"])
    except (TypeError, ValueError):
        return None

    self_name = clean_name(item.get("self") or "")
    self_active = bool(item.get("self_active"))
    active = []
    active_ids = []
    named_ids = {}
    participants = []

    def take_person(entry, into_active):
        nonlocal self_name, self_active
        if isinstance(entry, str):
            name = clean_name(entry)
            if name:
                if into_active:
                    active.append(name)
                else:
                    participants.append(name)
            return
        if not isinstance(entry, dict):
            return
        name = clean_name(entry.get("name") or "")
        ident = str(entry.get("id") or "").strip()
        is_self = bool(entry.get("self"))
        if is_self and name and not self_name:
            self_name = name
        if is_self and into_active:
            self_active = True
        if ident and name and not is_self:
            named_ids[ident] = name
        if into_active:
            if is_self:
                return
            if name:
                active.append(name)
            elif ident:
                active_ids.append(ident)
            return
        if name:
            participants.append(name)

    for entry in item.get("active") or []:
        take_person(entry, True)
    for ident in item.get("active_ids") or []:
        text = str(ident).strip()
        if text:
            active_ids.append(text)
    for entry in item.get("participants") or []:
        take_person(entry, False)
    if self_name:
        participants.append(self_name)
    raw_named = item.get("named_ids")
    if isinstance(raw_named, dict):
        for ident, name in raw_named.items():
            ident = str(ident).strip()
            name = clean_name(name)
            if ident and name:
                named_ids[ident] = name

    return {
        "t": moment,
        "active": _unique(active),
        "active_ids": _unique(active_ids),
        "self": self_name,
        "self_active": self_active,
        "participants": _unique(participants),
        "named_ids": named_ids,
    }


def load_speaker_events(path):
    """Load speakers.jsonl. Bad lines are skipped."""
    if not path or not os.path.isfile(path):
        return []
    events = []
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError:
        return []
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = coerce_event(item)
            if event is not None:
                events.append(event)
    events.sort(key=lambda event: event["t"])
    return events


def meet_self_name(events):
    for event in events:
        if event.get("self"):
            return event["self"]
    return ""


def choose_self_label(configured, events):
    """Use the Meet display name when the config still has the default."""
    configured = clean_name(configured) or "You"
    observed = meet_self_name(events)
    if configured == "You" and observed:
        return observed
    return configured


def _id_labels(events):
    """Map device ids to a Meet name, or else Speaker A/B in first-seen order."""
    named = {}
    for event in events:
        for ident, name in (event.get("named_ids") or {}).items():
            if name and ident not in named:
                named[ident] = name
    order = []
    for event in events:
        for ident in event.get("active_ids") or []:
            if ident not in named and ident not in order:
                order.append(ident)
        for ident in (event.get("named_ids") or {}):
            if ident not in named and ident not in order:
                order.append(ident)
    labels = dict(named)
    for index, ident in enumerate(order):
        labels[ident] = _diarized_label(index)
    return labels


def _diarized_label(index):
    """0 -> Speaker A, 25 -> Speaker Z, 26 -> Speaker AA."""
    letters = ""
    value = index
    while True:
        letters = chr(ord("A") + (value % 26)) + letters
        value = value // 26 - 1
        if value < 0:
            break
    return f"Speaker {letters}"


def _names_at(event, ignore, id_labels):
    names = []
    for name in event.get("active") or []:
        if name and name not in ignore:
            names.append(name)
    for ident in event.get("active_ids") or []:
        label = id_labels.get(ident) or ""
        if label and label not in ignore:
            names.append(label)
    return _unique(names)


def speaker_for_interval(start, end, events, ignore_names=None, id_labels=None, self_label=None):
    """Name that best covers [start, end).

    `ignore_names` are dropped (the local Meet user on the remote track).
    When `self_label` is set, those ignored names are counted as the local
    user instead. That is the mixed-audio path, where one file contains both.
    """
    ignore = set(ignore_names or [])
    id_labels = id_labels or {}
    start = float(start)
    end = float(end)
    if end <= start:
        end = start + 0.01
    span = end - start
    totals = {}
    ordered = sorted(events, key=lambda event: event["t"])
    for index, event in enumerate(ordered):
        begin = float(event["t"])
        if index + 1 < len(ordered):
            finish = float(ordered[index + 1]["t"])
        else:
            finish = begin + SAMPLE_COVER_SECONDS
        if finish - begin > SAMPLE_COVER_SECONDS:
            finish = begin + SAMPLE_COVER_SECONDS
        lo = max(start, begin)
        hi = min(end, finish)
        if hi <= lo:
            continue
        names = _names_at(event, ignore, id_labels)
        if not names and self_label and event.get("self_active"):
            names = [self_label]
        if not names:
            continue
        share = (hi - lo) / float(len(names))
        for name in names:
            totals[name] = totals.get(name, 0.0) + share
    if not totals:
        return THEM
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    top_name, top = ranked[0]
    if top < 0.2 * span and top < 0.3:
        return THEM
    if len(ranked) > 1:
        second_name, second = ranked[1]
        if second >= 0.45 * top and second >= 0.25 * span:
            return f"{top_name} / {second_name}"
    return top_name


def _overlap_fraction(left, right):
    lo = max(float(left["start"]), float(right["start"]))
    hi = min(float(left["end"]), float(right["end"]))
    if hi <= lo:
        return 0.0
    span = min(float(left["end"]) - float(left["start"]), float(right["end"]) - float(right["start"]))
    if span <= 0:
        return 0.0
    return (hi - lo) / span


def _similar(left, right):
    aa = clean_text(left).lower()
    bb = clean_text(right).lower()
    if not aa or not bb:
        return False
    if aa == bb:
        return True
    if len(aa) > 12 and (aa in bb or bb in aa):
        return True
    return SequenceMatcher(None, aa, bb).ratio() >= BLEED_SIMILARITY


def _unnamed(speaker, self_label):
    if not speaker or speaker == THEM or speaker == self_label:
        return True
    parts = [part.strip() for part in str(speaker).split(" / ")]
    return all(part in ("", THEM, self_label) for part in parts)


def resolve_cross_talk(self_segments, remote_segments, self_label):
    """Drop a duplicate when the same words landed on both tracks.

    A named remote line wins over the microphone copy (speaker bleed).
    An unnamed remote line loses to the microphone (sidetone / echo).
    """
    drop_self = set()
    drop_remote = set()
    for index, segment in enumerate(self_segments):
        for other_index, other in enumerate(remote_segments):
            if _overlap_fraction(segment, other) < BLEED_OVERLAP:
                continue
            if not _similar(segment.get("text"), other.get("text")):
                continue
            if _unnamed(other.get("speaker"), self_label):
                drop_remote.add(other_index)
            else:
                drop_self.add(index)
    kept_self = [segment for index, segment in enumerate(self_segments) if index not in drop_self]
    kept_remote = [segment for index, segment in enumerate(remote_segments) if index not in drop_remote]
    return kept_self, kept_remote


def _segment_tokens(segment, speaker, track):
    words = segment.get("words") or []
    usable = []
    for word in words:
        text = clean_text(word.get("text") or word.get("word"))
        if not text:
            continue
        usable.append(
            {
                "start": float(word.get("start", segment.get("start", 0.0))),
                "end": float(word.get("end", segment.get("end", 0.0))),
                "text": text,
            }
        )
    if not usable:
        text = clean_text(segment.get("text"))
        if not text:
            return []
        return [
            {
                "start": float(segment.get("start", 0.0)),
                "end": float(segment.get("end", 0.0)),
                "text": text,
                "speaker": speaker,
                "track": track,
            }
        ]
    tokens = []
    for word in usable:
        tokens.append(
            {
                "start": word["start"],
                "end": word["end"],
                "text": word["text"],
                "speaker": speaker,
                "track": track,
            }
        )
    return tokens


def group_turns(tokens, gap=TURN_GAP_SECONDS):
    """Merge consecutive tokens from the same speaker on the same track."""
    ordered = sorted(
        tokens,
        key=lambda item: (item["start"], 0 if item.get("track") == "self" else 1, item["end"]),
    )
    groups = []
    current = None
    for token in ordered:
        if (
            current is not None
            and current["speaker"] == token["speaker"]
            and current.get("track") == token.get("track")
            and float(token["start"]) - float(current["end"]) <= gap
        ):
            current["end"] = max(float(current["end"]), float(token["end"]))
            current["text"] = (current["text"] + " " + token["text"]).strip()
            continue
        current = {
            "start": float(token["start"]),
            "end": float(token["end"]),
            "text": token["text"],
            "speaker": token["speaker"],
            "track": token.get("track") or "",
        }
        groups.append(current)
    return groups


def align_tracks(self_segments, remote_segments, events, self_label):
    """Label microphone segments as `self_label` and remote segments from Meet."""
    self_label = clean_name(self_label) or "You"
    ignore = set()
    observed = meet_self_name(events)
    if observed:
        ignore.add(observed)
    if self_label:
        ignore.add(self_label)
    id_labels = _id_labels(events)

    remote_labeled = []
    for segment in remote_segments or []:
        text = clean_text(segment.get("text"))
        words = segment.get("words") or []
        if words:
            named_words = []
            for word in words:
                word_text = clean_text(word.get("text") or word.get("word"))
                if not word_text:
                    continue
                start = float(word.get("start", segment.get("start", 0.0)))
                end = float(word.get("end", segment.get("end", 0.0)))
                named_words.append(
                    {
                        "start": start,
                        "end": end,
                        "text": word_text,
                        "speaker": speaker_for_interval(
                            start, end, events, ignore_names=ignore, id_labels=id_labels
                        ),
                    }
                )
            if not named_words:
                continue
            # Majority name is what cross-talk compares; words keep their own.
            speaker = speaker_for_interval(
                float(segment.get("start", named_words[0]["start"])),
                float(segment.get("end", named_words[-1]["end"])),
                events,
                ignore_names=ignore,
                id_labels=id_labels,
            )
            remote_labeled.append(
                {
                    "start": float(segment.get("start", named_words[0]["start"])),
                    "end": float(segment.get("end", named_words[-1]["end"])),
                    "text": text or " ".join(word["text"] for word in named_words),
                    "speaker": speaker,
                    "words": named_words,
                    "track": "others",
                }
            )
            continue
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        remote_labeled.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "speaker": speaker_for_interval(
                    start, end, events, ignore_names=ignore, id_labels=id_labels
                ),
                "track": "others",
            }
        )

    self_plain = []
    for segment in self_segments or []:
        text = clean_text(segment.get("text"))
        if not text and not segment.get("words"):
            continue
        self_plain.append(dict(segment, text=text, speaker=self_label, track="self"))

    kept_self, kept_remote = resolve_cross_talk(self_plain, remote_labeled, self_label)
    tokens = []
    for segment in kept_self:
        tokens.extend(_segment_tokens(segment, self_label, "self"))
    for segment in kept_remote:
        words = segment.get("words") or []
        if words and all(word.get("speaker") for word in words):
            for word in words:
                tokens.append(
                    {
                        "start": float(word["start"]),
                        "end": float(word["end"]),
                        "text": word["text"],
                        "speaker": word["speaker"],
                        "track": "others",
                    }
                )
            continue
        tokens.extend(_segment_tokens(segment, segment.get("speaker") or THEM, "others"))
    return group_turns(tokens)


def align_mixed(segments, events, self_label):
    """Label a single mixed recording from the Meet active-speaker timeline."""
    self_label = choose_self_label(self_label, events)
    observed = meet_self_name(events)
    ignore = set()
    if observed:
        ignore.add(observed)
    id_labels = _id_labels(events)
    tokens = []
    for segment in segments or []:
        words = segment.get("words") or []
        if words:
            for word in words:
                text = clean_text(word.get("text") or word.get("word"))
                if not text:
                    continue
                start = float(word.get("start", segment.get("start", 0.0)))
                end = float(word.get("end", segment.get("end", 0.0)))
                tokens.append(
                    {
                        "start": start,
                        "end": end,
                        "text": text,
                        "speaker": speaker_for_interval(
                            start,
                            end,
                            events,
                            ignore_names=ignore,
                            id_labels=id_labels,
                            self_label=self_label,
                        ),
                        "track": "mix",
                    }
                )
            continue
        text = clean_text(segment.get("text"))
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        tokens.append(
            {
                "start": start,
                "end": end,
                "text": text,
                "speaker": speaker_for_interval(
                    start,
                    end,
                    events,
                    ignore_names=ignore,
                    id_labels=id_labels,
                    self_label=self_label,
                ),
                "track": "mix",
            }
        )
    return group_turns(tokens)


def format_timestamp(seconds):
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    remain = int(seconds % 60)
    return f"{minutes:02d}:{remain:02d}"


def render_transcript(segments, labeled=True):
    """`[MM:SS] Name: text` lines. Unlabeled lines omit the name."""
    lines = []
    for segment in segments or []:
        text = clean_text(segment.get("text"))
        if not text:
            continue
        stamp = format_timestamp(segment.get("start", 0.0))
        speaker = clean_name(segment.get("speaker"))
        if labeled and speaker:
            lines.append(f"[{stamp}] {speaker}: {text}")
        else:
            lines.append(f"[{stamp}] {text}")
    return "\n".join(lines)


def _split_speakers(speaker):
    return [part.strip() for part in str(speaker).split(" / ") if part.strip()]


def _is_generic(name):
    return name in ("", THEM) or name.startswith("Speaker ")


def participant_list(self_label, segments, events):
    """self label first, then roster order, then anyone who actually spoke."""
    self_label = clean_name(self_label) or "You"
    observed = meet_self_name(events)
    names = [self_label]
    for event in events or []:
        for name in event.get("participants") or []:
            if not name or name == observed or name == self_label or name in names:
                continue
            if name == THEM:
                continue
            names.append(name)
    for segment in segments or []:
        for part in _split_speakers(segment.get("speaker")):
            if part == THEM or part in names:
                continue
            names.append(part)
    return names


def classify_source(segments, events, used_tracks, used_mix, self_label=""):
    labels = []
    for segment in segments or []:
        labels.extend(_split_speakers(segment.get("speaker")))
    self_labels = {clean_name(self_label), clean_name(meet_self_name(events))}
    self_labels.discard("")
    remote = [
        label
        for label in labels
        if label and label not in self_labels and not _is_generic(label)
    ]
    if remote and events:
        return "meet"
    if any(str(label).startswith("Speaker ") for label in labels):
        return "diarized"
    if used_tracks:
        return "tracks"
    if used_mix:
        return "mix"
    return "plain"


def build_document(segments, events, self_label, source):
    rounded = []
    for segment in segments or []:
        rounded.append(
            {
                "start": round(float(segment["start"]), 3),
                "end": round(float(segment["end"]), 3),
                "speaker": segment.get("speaker") or THEM,
                "text": clean_text(segment.get("text")),
                "track": segment.get("track") or "",
            }
        )
    observed = meet_self_name(events)
    return {
        "self_name": self_label,
        "meet_self_name": observed or None,
        "participants": participant_list(self_label, rounded, events),
        "speaker_source": source,
        "limits": list(SPEAKER_LIMITS),
        "segments": rounded,
    }


def speaker_webhook_fields(folder):
    """Fields added to the webhook body. Empty when there is no transcript.json."""
    path = os.path.join(folder, TRANSCRIPT_JSON)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    speakers = []
    for segment in document.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start"))
            end = float(segment.get("end"))
        except (TypeError, ValueError):
            continue
        speakers.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "speaker": segment.get("speaker") or THEM,
                "text": clean_text(segment.get("text")),
            }
        )
    participants = document.get("participants")
    if not isinstance(participants, list):
        participants = []
    participants = [clean_name(name) for name in participants if clean_name(name)]
    fields = {
        "self_name": clean_name(document.get("self_name")) or "You",
        "participants": participants,
        "speakers": speakers,
    }
    source = document.get("speaker_source")
    if source:
        fields["speaker_source"] = str(source)
    return fields


def _existing_track(folder, name):
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) <= 44:
            return None
    except OSError:
        return None
    if not track_has_speech(path):
        return None
    return path


def produce_transcript(folder, self_name, enabled, transcribe_plain, transcribe_paths):
    """Return `(text, document_or_none)`.

    With labeling off, or with nothing but a mixed file and no speaker log,
    the text is whatever `transcribe_plain` returns and the document is None.
    That keeps older sessions and the unlabeled config on the historical line
    format.
    """
    audio = os.path.join(folder, "audio.wav")
    if not enabled:
        return transcribe_plain(audio), None

    events = load_speaker_events(os.path.join(folder, SPEAKER_LOG))
    self_label = choose_self_label(self_name, events)
    self_path = _existing_track(folder, SELF_TRACK)
    others_path = _existing_track(folder, OTHERS_TRACK)

    if self_path or others_path:
        paths = []
        roles = []
        if self_path:
            paths.append(self_path)
            roles.append("self")
        if others_path:
            paths.append(others_path)
            roles.append("others")
        detailed = transcribe_paths(paths)
        by_role = {}
        for role, segments in zip(roles, detailed):
            by_role[role] = segments or []
        labeled = align_tracks(
            by_role.get("self") or [],
            by_role.get("others") or [],
            events,
            self_label,
        )
        source = classify_source(
            labeled, events, used_tracks=True, used_mix=False, self_label=self_label
        )
        document = build_document(labeled, events, self_label, source)
        return render_transcript(labeled, labeled=True), document

    if events:
        detailed = transcribe_paths([audio])
        segments = detailed[0] if detailed else []
        labeled = align_mixed(segments, events, self_label)
        source = classify_source(
            labeled, events, used_tracks=False, used_mix=True, self_label=self_label
        )
        document = build_document(labeled, events, self_label, source)
        return render_transcript(labeled, labeled=True), document

    return transcribe_plain(audio), None
