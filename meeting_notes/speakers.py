"""Attribute transcript lines to people.

The microphone track is the local user (`self_name`). System-audio loopback is
everyone else, mixed into one file. Remote words are named from Meet live
captions: the caption speaker on screen at that time. If no caption covers
the stretch, the active-speaker tile is used when it shows exactly one
person. Otherwise the line is "Remote" (or "Them" when the extension sent no
roster). Distinct people are never glued into one "Ada / Grace" label.

A two-person call has one remote name, so that name is used when captions
are off. Voice diarization (`diarize.py`) runs only when `diarize` is set.
Stable anonymous ids still become "Speaker A", "Speaker B", and so on.

Turns are built per track and speaker before the tracks are interleaved, so
overlapping speech stays whole sentences instead of alternating single words.

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
REMOTE = "Remote"
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
    "Remote audio is one system-loopback mix. Each stretch is named from the Meet caption speaker at that time. With captions off, a single active tile is used. If neither names one person, the line is Remote. Distinct people are not merged into one combined label.",
    "A two-person call has one remote person. That name is used when no caption covers the stretch.",
    "A conference-room speakerphone is a single Meet name, not each person around it.",
    "Names are read from the Meet tab while it is open, including live captions when the user has turned them on. Captions being off is normal. A background tab, no Chrome extension, or a Meet UI change falls back to Them, Speaker 2, or Speaker A/B.",
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

    captions = []
    raw_captions = item.get("captions")
    if isinstance(raw_captions, list):
        for entry in raw_captions[:8]:
            if not isinstance(entry, dict):
                continue
            speaker = clean_name(entry.get("speaker") or entry.get("name"))[:80]
            text = clean_text(entry.get("text"))[:600]
            if speaker and text:
                captions.append({"speaker": speaker, "text": text})
    # Debug field written on every sample so a log shows who the caption
    # reader saw, including an empty string when captions are off.
    caption_speaker = clean_name(item.get("caption_speaker") or "")[:80]
    if not caption_speaker and len(captions) == 1:
        caption_speaker = captions[0]["speaker"]

    return {
        "t": moment,
        "active": _unique(active),
        "active_ids": _unique(active_ids),
        "self": self_name,
        "self_active": self_active,
        "participants": _unique(participants),
        "named_ids": named_ids,
        "captions": captions,
        "caption_speaker": caption_speaker,
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


def _ignore_names(events, self_label):
    ignore = {"", "You", THEM}
    label = clean_name(self_label)
    if label:
        ignore.add(label)
    observed = meet_self_name(events)
    if observed:
        ignore.add(observed)
    return ignore


def _event_bounds(events):
    ordered = sorted(events or [], key=lambda event: event["t"])
    bounds = []
    for index, event in enumerate(ordered):
        begin = float(event["t"])
        if index + 1 < len(ordered):
            finish = float(ordered[index + 1]["t"])
        else:
            finish = begin + SAMPLE_COVER_SECONDS
        if finish - begin > SAMPLE_COVER_SECONDS:
            finish = begin + SAMPLE_COVER_SECONDS
        if finish <= begin:
            finish = begin + 0.01
        bounds.append((begin, finish))
    return ordered, bounds


def remote_parties(events, self_label):
    """Remote display names, then anonymous Meet ids, excluding the local user.

    Caption speakers count as names. They are how a call is recognized as
    having two or more remote people when the tile highlight is wrong.
    """
    ignore = _ignore_names(events, self_label)
    names = []
    for event in events or []:
        seen = []
        seen.extend(event.get("participants") or [])
        seen.extend(event.get("active") or [])
        for caption in event.get("captions") or []:
            seen.append(caption.get("speaker"))
        for name in seen:
            if name and name not in ignore and name not in names:
                names.append(name)
    id_labels = _id_labels(events)
    anons = []
    named_ids = set()
    for event in events or []:
        named_ids.update((event.get("named_ids") or {}).keys())
    for event in events or []:
        for ident in event.get("active_ids") or []:
            if ident in named_ids:
                continue
            label = id_labels.get(ident) or ""
            if not label or label in ignore or label in names or label in anons:
                continue
            anons.append(label)
    return names, anons


def single_remote_name(events, self_label):
    """The one other person on a two-person call, or "" when that is not known."""
    names, anons = remote_parties(events, self_label)
    if len(names) == 1 and not anons:
        return names[0]
    return ""


def _interval_overlap(start, end, begin, finish):
    lo = max(float(start), float(begin))
    hi = min(float(end), float(finish))
    if hi <= lo:
        return 0.0
    return hi - lo


def live_caption_intervals(events, self_label):
    """Time ranges owned by the caption speaker active then.

    `caption_speaker` on a sample is that person until the next sample.
    Older logs that only stored caption rows use the row whose text changed.
    Samples where two rows change at once are skipped. No captions yields [].
    """
    ignore = _ignore_names(events, self_label)
    ordered, bounds = _event_bounds(events)
    last_text = {}
    raw = []
    for event, (begin, finish) in zip(ordered, bounds):
        explicit = clean_name(event.get("caption_speaker") or "")
        if explicit and explicit not in ignore:
            raw.append({"start": begin, "end": finish, "speaker": explicit})
            for caption in event.get("captions") or []:
                speaker = caption.get("speaker") or ""
                text = caption.get("text") or ""
                if speaker and text:
                    last_text[speaker] = text
            continue
        changed = []
        for caption in event.get("captions") or []:
            speaker = caption.get("speaker") or ""
            text = caption.get("text") or ""
            if not speaker or not text or speaker in ignore:
                continue
            previous = last_text.get(speaker)
            if previous is None or text != previous:
                changed.append(speaker)
            last_text[speaker] = text
        changed = _unique(changed)
        if len(changed) != 1:
            continue
        raw.append({"start": begin, "end": finish, "speaker": changed[0]})
    return _merge_named_intervals(raw)


def caption_status(events):
    """`on` when any sample saw a caption, `off` when samples had none, else ""."""
    if not events:
        return ""
    for event in events:
        if clean_name(event.get("caption_speaker") or ""):
            return "on"
        if event.get("captions"):
            return "on"
    return "off"


def _has_multiple_remotes(events, self_label):
    names, anons = remote_parties(events, self_label)
    return len(names) + len(anons) >= 2


def label_remote_interval(start, end, events, self_label, caption_intervals, id_labels, mixed=False):
    """Name one remote stretch. Never a combined "A / B" label.

    Captions first, then the only other person on a two-person call, then a
    tile that shows exactly one remote speaker, then Remote or Them.
    """
    self_label = clean_name(self_label) or "You"
    caption = caption_speaker_for_interval(start, end, caption_intervals)
    if caption:
        return caption
    if not mixed:
        solo = single_remote_name(events, self_label)
        if solo:
            return solo
    ignore = set()
    observed = meet_self_name(events)
    if observed:
        ignore.add(observed)
    if not mixed and self_label:
        ignore.add(self_label)
    label = speaker_for_interval(
        start,
        end,
        events,
        ignore_names=ignore,
        id_labels=id_labels,
        self_label=self_label if mixed else None,
    )
    if " / " in str(label):
        return REMOTE
    if label and label != THEM:
        return label
    if _has_multiple_remotes(events, self_label):
        return REMOTE
    return THEM


def exclusive_tile_intervals(events, self_label):
    """Ranges where Meet marked exactly one remote participant as speaking."""
    ignore = _ignore_names(events, self_label)
    id_labels = _id_labels(events)
    ordered, bounds = _event_bounds(events)
    raw = []
    for event, (begin, finish) in zip(ordered, bounds):
        names = _names_at(event, ignore, id_labels)
        if len(names) != 1:
            continue
        raw.append({"start": begin, "end": finish, "speaker": names[0]})
    return _merge_named_intervals(raw)


def _merge_named_intervals(raw):
    merged = []
    for item in raw:
        if (
            merged
            and merged[-1]["speaker"] == item["speaker"]
            and float(item["start"]) <= float(merged[-1]["end"]) + 0.35
        ):
            merged[-1]["end"] = max(float(merged[-1]["end"]), float(item["end"]))
            continue
        merged.append(
            {"start": float(item["start"]), "end": float(item["end"]), "speaker": item["speaker"]}
        )
    return merged


def _score_clusters(cluster_segments, intervals):
    scores = {}
    for segment in cluster_segments:
        cluster = segment["cluster"]
        bucket = scores.setdefault(cluster, {})
        for interval in intervals:
            overlap = _interval_overlap(
                segment["start"], segment["end"], interval["start"], interval["end"]
            )
            if overlap <= 0:
                continue
            name = interval["speaker"]
            bucket[name] = bucket.get(name, 0.0) + overlap
    return scores


def _confident_name(score_map, minimum):
    if not score_map:
        return None
    ranked = sorted(score_map.items(), key=lambda item: (-item[1], item[0]))
    name, best = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if best < minimum:
        return None
    if second and best < 2.0 * second:
        return None
    if " / " in name:
        return None
    return name, best


def name_voice_clusters(voice_segments, events, self_label):
    """Map each voice cluster to one person, or to Speaker 2, Speaker 3, ...

    Captions are trusted over tiles. A name is used for at most one cluster.
    Nothing is guessed from the roster when two people could fit: an unnamed
    cluster stays Speaker N instead of becoming "Ada / Grace".
    """
    self_label = clean_name(self_label) or "You"
    clusters = []
    first_start = {}
    for segment in voice_segments or []:
        cluster = str(segment.get("cluster"))
        if cluster not in first_start:
            first_start[cluster] = float(segment["start"])
            clusters.append(cluster)
        else:
            first_start[cluster] = min(first_start[cluster], float(segment["start"]))
    if not clusters:
        return {}
    caption_scores = _score_clusters(voice_segments, live_caption_intervals(events, self_label))
    tile_scores = _score_clusters(voice_segments, exclusive_tile_intervals(events, self_label))
    candidates = []
    caption_hits = set()
    for cluster in clusters:
        picked = _confident_name(caption_scores.get(cluster), 0.45)
        if picked is None:
            continue
        name, score = picked
        candidates.append((0, -score, cluster, name))
        caption_hits.add(cluster)
    for cluster in clusters:
        if cluster in caption_hits:
            continue
        picked = _confident_name(tile_scores.get(cluster), 0.8)
        if picked is None:
            continue
        name, score = picked
        candidates.append((1, -score, cluster, name))
    candidates.sort()
    assigned = {}
    used = set()
    roster, _anons = remote_parties(events, self_label)
    for _priority, _score, cluster, name in candidates:
        canonical = name
        for item in roster:
            if item.casefold() == name.casefold():
                canonical = item
                break
        if cluster in assigned or canonical in used or canonical in _ignore_names(events, self_label):
            continue
        assigned[cluster] = canonical
        used.add(canonical)
    unnamed = [cluster for cluster in clusters if cluster not in assigned]
    unnamed.sort(key=lambda cluster: (first_start[cluster], cluster))
    number = 2
    for cluster in unnamed:
        while f"Speaker {number}" in used:
            number += 1
        label = f"Speaker {number}"
        assigned[cluster] = label
        used.add(label)
        number += 1
    return assigned


def cluster_for_interval(start, end, voice_segments, snap=0.45):
    """Cluster id with the greatest overlap, else the nearest within `snap`."""
    if not voice_segments:
        return None
    start = float(start)
    end = float(end)
    if end <= start:
        end = start + 0.01
    best = None
    best_overlap = 0.0
    for segment in voice_segments:
        overlap = _interval_overlap(start, end, segment["start"], segment["end"])
        if overlap > best_overlap or (
            overlap == best_overlap
            and overlap > 0
            and best is not None
            and float(segment["start"]) < float(best["start"])
        ):
            best = segment
            best_overlap = overlap
    if best is not None and best_overlap > 0:
        return str(best["cluster"])
    midpoint = (start + end) / 2.0
    nearest = None
    distance = float(snap) + 1.0
    limit = float(snap)
    for segment in voice_segments:
        if midpoint < float(segment["start"]):
            gap = float(segment["start"]) - midpoint
        elif midpoint > float(segment["end"]):
            gap = midpoint - float(segment["end"])
        else:
            gap = 0.0
        if gap > limit:
            continue
        if nearest is None or gap < distance or (
            gap == distance and float(segment["start"]) < float(nearest["start"])
        ):
            distance = gap
            nearest = segment
    if nearest is None:
        return None
    return str(nearest["cluster"])


def caption_speaker_for_interval(start, end, intervals, minimum=0.05):
    """Remote caption speaker covering this span, when one person dominates."""
    totals = {}
    for interval in intervals or []:
        overlap = _interval_overlap(start, end, interval["start"], interval["end"])
        if overlap <= 0:
            continue
        name = interval["speaker"]
        totals[name] = totals.get(name, 0.0) + overlap
    if not totals:
        return ""
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    name, best = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if best < minimum:
        return ""
    if second and best < 2.0 * second:
        return ""
    return name


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
    if not speaker or speaker in (THEM, REMOTE) or speaker == self_label:
        return True
    parts = [part.strip() for part in str(speaker).split(" / ")]
    return all(part in ("", THEM, REMOTE, self_label) for part in parts)


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
    """Build one turn per track and speaker, then order the turns by time.

    Someone else talking does not split a sentence. A turn breaks only when
    that same speaker on that same track has been silent for longer than
    `gap`. A short interjection from the other person stays its own line, and
    the sentence underneath stays intact.
    """
    buckets = {}
    for token in tokens or []:
        key = (token.get("track") or "", token.get("speaker") or "")
        buckets.setdefault(key, []).append(token)
    groups = []
    for items in buckets.values():
        items.sort(key=lambda item: (float(item["start"]), float(item["end"])))
        current = None
        for token in items:
            start = float(token["start"])
            end = float(token["end"])
            if current is not None and start - float(current["end"]) <= gap:
                current["end"] = max(float(current["end"]), end)
                current["text"] = (current["text"] + " " + token["text"]).strip()
                continue
            current = {
                "start": start,
                "end": end,
                "text": token["text"],
                "speaker": token.get("speaker") or "",
                "track": token.get("track") or "",
            }
            groups.append(current)
    groups.sort(
        key=lambda item: (item["start"], 0 if item.get("track") == "self" else 1, item["end"])
    )
    return groups


def align_tracks(self_segments, remote_segments, events, self_label, voice_segments=None):
    """Label microphone segments as `self_label` and remote segments from captions.

    Captions name the remote stretch when they cover it. A single active tile
    is next, then Remote. `voice_segments`, when diarization was turned on,
    replace that with one name per voice and still never a combined label.
    """
    self_label = clean_name(self_label) or "You"
    id_labels = _id_labels(events)
    caption_intervals = live_caption_intervals(events, self_label)
    voice_names = None
    if voice_segments:
        voice_names = name_voice_clusters(voice_segments, events, self_label)

    def name_remote(start, end):
        if voice_names:
            cluster = cluster_for_interval(start, end, voice_segments)
            if cluster is not None and voice_names.get(cluster):
                return voice_names[cluster]
        return label_remote_interval(
            start,
            end,
            events,
            self_label,
            caption_intervals,
            id_labels,
            mixed=False,
        )

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
                        "speaker": name_remote(start, end),
                    }
                )
            if not named_words:
                continue
            # Majority name is what cross-talk compares; words keep their own.
            speaker = name_remote(
                float(segment.get("start", named_words[0]["start"])),
                float(segment.get("end", named_words[-1]["end"])),
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
                "speaker": name_remote(start, end),
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
    """Label a single mixed recording. Captions, then one tile, never "A / B"."""
    self_label = choose_self_label(self_label, events)
    id_labels = _id_labels(events)
    caption_intervals = live_caption_intervals(events, self_label)
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
                        "speaker": label_remote_interval(
                            start,
                            end,
                            events,
                            self_label,
                            caption_intervals,
                            id_labels,
                            mixed=True,
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
                "speaker": label_remote_interval(
                    start,
                    end,
                    events,
                    self_label,
                    caption_intervals,
                    id_labels,
                    mixed=True,
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
    return name in ("", THEM, REMOTE) or name.startswith("Speaker ")


def participant_list(self_label, segments, events):
    """self label first, then roster order, then anyone who actually spoke."""
    self_label = clean_name(self_label) or "You"
    observed = meet_self_name(events)
    names = [self_label]
    for event in events or []:
        for name in event.get("participants") or []:
            if not name or name == observed or name == self_label or name in names:
                continue
            if name in (THEM, REMOTE):
                continue
            names.append(name)
    for segment in segments or []:
        for part in _split_speakers(segment.get("speaker")):
            if part in (THEM, REMOTE) or part in names:
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


def build_document(segments, events, self_label, source, diarization=None):
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
    document = {
        "self_name": self_label,
        "meet_self_name": observed or None,
        "participants": participant_list(self_label, rounded, events),
        "speaker_source": source,
        "limits": list(SPEAKER_LIMITS),
        "segments": rounded,
    }
    if diarization:
        document["diarization"] = diarization
    status = caption_status(events)
    if status:
        document["captions"] = status
    return document


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


def _needs_remote_diarization(events, self_label):
    names, anons = remote_parties(events, self_label)
    return len(names) + len(anons) >= 2


def _voice_timeline(folder, events, self_label, diarize, diarize_backend, diarize_max_speakers, hf_token, diarizer):
    """Cluster the remote mix only when diarization was explicitly turned on.

    A two-person call (one remote name) does not go through this. The default
    path never imports the diarizer, so numpy and the model packages stay
    optional.
    """
    if not diarize:
        return None, None
    if not _needs_remote_diarization(events, self_label):
        return None, None
    others = _existing_track(folder, OTHERS_TRACK)
    if not others:
        return None, {
            "backend": None,
            "clusters": 0,
            "reason": "no remote audio track",
        }
    if diarizer is None:
        from meeting_notes.diarize import diarize_wav

        def diarizer(path):
            return diarize_wav(
                path,
                backend=diarize_backend,
                hf_token=hf_token,
                max_speakers=diarize_max_speakers,
            )

    try:
        result = diarizer(others)
    except Exception as exc:
        text = " ".join(str(exc).split())
        token = str(hf_token or "").strip()
        if token and token in text:
            text = text.replace(token, "***")
        return None, {"backend": None, "clusters": 0, "reason": text[:180] or "diarization failed"}
    segments = []
    backend = "custom"
    reason = ""
    if isinstance(result, dict):
        backend = result.get("backend") or "custom"
        reason = str(result.get("reason") or "")
        result = result.get("segments") or []
    if result:
        for item in result:
            if not isinstance(item, dict):
                continue
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (TypeError, ValueError, KeyError):
                continue
            cluster = str(item.get("cluster") if item.get("cluster") is not None else "")
            if cluster and end > start:
                segments.append({"start": start, "end": end, "cluster": cluster})
    if not segments:
        return None, {
            "backend": None,
            "clusters": 0,
            "reason": reason or "diarization unavailable",
        }
    clusters = sorted({segment["cluster"] for segment in segments})
    return segments, {"backend": str(backend), "clusters": len(clusters)}


def produce_transcript(
    folder,
    self_name,
    enabled,
    transcribe_plain,
    transcribe_paths,
    diarize=False,
    diarize_backend="auto",
    diarize_max_speakers=8,
    hf_token="",
    diarizer=None,
):
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
        voice_segments, diarization = _voice_timeline(
            folder,
            events,
            self_label,
            diarize,
            diarize_backend,
            diarize_max_speakers,
            hf_token,
            diarizer,
        )
        labeled = align_tracks(
            by_role.get("self") or [],
            by_role.get("others") or [],
            events,
            self_label,
            voice_segments=voice_segments,
        )
        source = classify_source(
            labeled, events, used_tracks=True, used_mix=False, self_label=self_label
        )
        document = build_document(labeled, events, self_label, source, diarization=diarization)
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
