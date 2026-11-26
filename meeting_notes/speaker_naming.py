"""Put real names on remote voices, with a confidence per voice.

The microphone track is always the user (`self_name`). The remote track is
split into voices by `diarize.py` ("Speaker 2", "Speaker 3", ...). This module
decides who each voice is, from evidence only:

- roster: calendar attendees (ICS or a calendar.json drop file), names in the
  Meet window title ("Partnerships Sync (Alex Rivera)"), a Slack DM or
  group-DM title, an explicit `roster.json` in the session folder, and Meet
  captions / tiles when the Chrome extension provides them;
- what was said: self-introductions ("Hi, this is Alex from Northwind"),
  people addressing each other by name ("Thanks, Priya" right before Priya
  answers, "Yes Mahmoud," right after Mahmoud spoke), and the rule that
  nobody addresses themselves;
- elimination: one remote voice and one remote attendee, or one voice and one
  attendee left after the others were named;
- voice profiles of people confirmed in earlier calls (`voice_profiles.py`).

A voice is named only at confidence >= NAME_THRESHOLD. Otherwise it stays
"Speaker N", or "Speaker N (unconfirmed: likely X)" when there is weaker
evidence for X. A name that was never said, listed or confirmed is never
produced.
"""

import json
import os
import re

NAME_THRESHOLD = 0.7
HINT_THRESHOLD = 0.3
GENERIC_REMOTE = ("Them", "Remote", "Unattributed")

# Words that follow "I'm" / "this is" / sit in a vocative slot but are not
# names. Whisper capitalises sentence starts, so this list matters.
NOT_NAMES = {
    word.casefold()
    for word in """
    I Im I'm OK Okay Sure Good Great Fine Here There Not Just So Yes Yeah Yep No Nope The
    Thanks Thank Sorry Back Happy Glad Excited Ready Done Going Gonna Trying Looking Also
    Actually Basically Really Very Well Hey Hi Hello Bye Right Cool Nice Perfect Exactly
    Absolutely Definitely Totally Correct Alright All Everyone Everybody Guys Folks Team
    Today Tomorrow Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February
    March April May June July August September October November December And But Or If
    When What Why How Who Where This That These Those It Its We You They He She Our Your
    Their My Me Us Them A An One Two Three Let Lets Let's Please Maybe Probably Kind Sort
    Coming Calling Speaking Talking Working Joining Muted Unmuted Late Early Sharing Here's
    Google Meet Slack Zoom Huddle Bebop Base Coinbase Greek Ethereum Bitcoin Solana DeFi
    Options Vault Vaults Hyperliquid Uniswap Aave Northwind Telegram Notion Discord Twitter
    """.split()
}

_NAME = r"([A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?)"
SELF_INTRO_PATTERNS = [
    re.compile(r"(?i:\b(?:my name is|my name's)\s+)" + _NAME),
    re.compile(r"(?i:\b(?:this is|it's|it is)\s+)" + _NAME + r"(?=\s+(?i:from|at|with|here|speaking)\b|[.,!]|$)"),
    re.compile(r"(?i:\b(?:I'm|I am|Im)\s+)" + _NAME + r"(?=\s+(?i:from|at|with|and I|here)\b|[.,!]|$)"),
    re.compile(
        r"(?i:^(?:(?:hi|hey|hello|yo)(?:\s+(?:all|everyone|everybody|guys|team|folks))?[,!.]?\s+)?(?:it's\s+)?)"
        + _NAME
        + r"(?i:\s+here)\b"
    ),
]


def clean_name(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def first_name(name):
    parts = clean_name(name).split()
    return parts[0] if parts else ""


def plausible_person(name):
    name = clean_name(name)
    if not name or len(name) > 40:
        return False
    words = name.split()
    if len(words) > 3:
        return False
    for word in words:
        if word.casefold().strip("'") in NOT_NAMES:
            return False
        if not word[0].isupper() or not re.match(r"^[A-Za-z][A-Za-z'\-\.]*$", word):
            return False
    return True


def is_generic_label(label):
    label = clean_name(label)
    return label in GENERIC_REMOTE or label.startswith("Speaker ")


# --- roster ------------------------------------------------------------

def roster_entry(name, source, strength=1.0, email=""):
    return {"name": clean_name(name), "source": source, "strength": float(strength), "email": email or ""}


def names_from_window_title(title, self_aliases=(), organizations=()):
    """Roster guesses from a Meet or Slack window title.

    - "Meet - Partnerships Sync (Alex Rivera)" -> Alex Rivera (strong)
    - "Mahmoud Ali (DM) - Greek - Slack" -> Mahmoud Ali (strong)
    - "Alex, Mahmoud - Greek - Slack" -> both (strong, group DM)
    - "Lukas <> Alex" -> Alex (weak; could be a company)
    - "executive - Greek - Slack" -> nothing (a channel)
    """
    title = clean_name(title)
    if not title:
        return []
    skip = {clean_name(a).casefold() for a in self_aliases if clean_name(a)}
    skip |= {clean_name(o).casefold() for o in organizations if clean_name(o)}
    found = []

    def add(name, strength, source):
        name = clean_name(name).strip(" -–|")
        if not plausible_person(name) or name.casefold() in skip:
            return
        if first_name(name).casefold() in skip:
            return
        if any(item["name"].casefold() == name.casefold() for item in found):
            return
        found.append(roster_entry(name, source, strength))

    body = re.sub(r"^\s*(?:Meet|Google Meet)\s*[-–]\s*", "", title)
    if body.endswith(" - Slack") or " - Slack" in body:
        head = body.split(" - ")[0]
        dm = re.match(r"^(.*?)\s*\(DM\)\s*$", head)
        if dm:
            add(dm.group(1), 1.0, "slack-dm")
            return found
        if "," in head:
            for part in head.split(","):
                add(part, 1.0, "slack-group-dm")
            return found
        return found
    for inner in re.findall(r"\(([^()]+)\)", body):
        for part in re.split(r",|&| and ", inner):
            add(part, 0.9, "meeting-title")
    if "<>" in body or " x " in body or " X " in body:
        stripped = re.sub(r"\([^()]*\)", "", body)
        for part in re.split(r"<>| x | X |/|\|", stripped):
            add(part, 0.5, "meeting-title-guess")
    return found


def load_roster_file(folder):
    """`roster.json` in the session folder: ["Name", ...] or [{"name":..}]."""
    path = os.path.join(folder, "roster.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("attendees") or data.get("participants") or []
    entries = []
    for item in data or []:
        if isinstance(item, str):
            entries.append(roster_entry(item, "roster-file"))
        elif isinstance(item, dict) and item.get("name"):
            entries.append(roster_entry(item["name"], item.get("source") or "roster-file", 1.0, item.get("email", "")))
    return entries


def merge_roster(*groups, self_aliases=()):
    skip = {clean_name(a).casefold() for a in self_aliases if clean_name(a)}
    merged = []
    for group in groups:
        for entry in group or []:
            name = clean_name(entry.get("name"))
            if not name or name.casefold() in skip:
                continue
            existing = None
            for item in merged:
                same = item["name"].casefold() == name.casefold()
                # "Alex" from a title and "Alex Rivera" from the calendar.
                partial = first_name(item["name"]).casefold() == name.casefold() or first_name(name).casefold() == item["name"].casefold()
                if same or partial:
                    existing = item
                    break
            if existing is None:
                merged.append(dict(entry, name=name))
                continue
            if len(name) > len(existing["name"]):
                existing["name"] = name
            if entry.get("strength", 0) > existing.get("strength", 0):
                existing["strength"] = entry["strength"]
                existing["source"] = entry.get("source", existing["source"])
    return merged


# --- evidence ----------------------------------------------------------

def self_introductions(text):
    """Names someone gives for themselves in `text`."""
    names = []
    text = clean_name(text)
    for pattern in SELF_INTRO_PATTERNS:
        for match in pattern.finditer(text):
            candidate = clean_name(match.group(1))
            # "I'm Alex from Northwind" -> keep "Alex", not "Alex From".
            words = [w for w in candidate.split() if w.casefold() not in NOT_NAMES]
            candidate = " ".join(words)
            if plausible_person(candidate) and candidate not in names:
                names.append(candidate)
    return names


_LEAD = r"(?:hey|hi|hello|yes|yeah|yep|thanks|thank you|ok|okay|sure|absolutely|perfect|great|so|well|no|right|exactly)"
_ONE = r"([A-Z][a-z][a-zA-Z'\-]+)"
VOCATIVE_PATTERNS = [
    re.compile(r"(?i:^" + _LEAD + r"[,!.]?\s+)" + _ONE + r"[,!.?]"),
    re.compile(r"^" + _ONE + r",\s+\w"),
    re.compile(r"[.?!]\s+" + _ONE + r",\s+\w"),
    re.compile(r",\s+" + _ONE + r"\s*[?.!]?\s*$"),
]


def vocative_names(text):
    """Names someone is addressed by in `text` ("Hey Alex," / "Ravi, could you")."""
    found = []
    text = clean_name(text)
    for pattern in VOCATIVE_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(1)
            if plausible_person(name) and name not in found:
                found.append(name)
    return found


def _vocative_positions(text, name):
    """Where `name` (or its first name) is used to address someone in `text`.

    Returns a list of "start" / "end" / "middle".
    """
    text = clean_name(text)
    hits = []
    options = {clean_name(name), first_name(name)}
    for option in options:
        if not option:
            continue
        token = re.escape(option)
        start = re.compile(r"(?i:^(?:(?:yes|yeah|yep|no|sure|thanks|thank you|hey|hi|hello|ok|okay|so|and|well|right|great|perfect|absolutely|exactly|good question)[,!.]?\s+)?)" + token + r"\b[,!.?]")
        end = re.compile(r"(?i:(?:,|\b(?:thanks|thank you|hey|hi|right|so|and|over to you|what about you|how about you|you))\s+)" + token + r"\s*[?.!]?\s*$")
        question = re.compile(r"(?:^|[.?!]\s+)" + token + r",\s+[^.?!]*\?")
        # "Perfect. Alex, one more question about ..." - a later sentence
        # that opens with the name hands the floor to that person.
        handoff = re.compile(r"[.?!]\s+" + token + r",\s+\w")
        middle = re.compile(r"(?i:(?:,\s+|\bthanks\s+|\bthank you\s+))" + token + r"[,.!?]")
        if start.search(text):
            hits.append("start")
        if end.search(text) or question.search(text) or handoff.search(text):
            hits.append("end")
        elif middle.search(text) and "start" not in hits:
            hits.append("middle")
    return hits


def _combine(weights):
    product = 1.0
    for weight in weights:
        product *= 1.0 - max(0.0, min(0.99, weight))
    return 1.0 - product


class Evidence:
    def __init__(self):
        self.positive = {}  # (label, name) -> [(weight, why)]
        self.negative = {}  # (label, name) -> [(weight, why)]

    def add(self, label, name, weight, why):
        self.positive.setdefault((label, name), []).append((float(weight), why))

    def deny(self, label, name, weight, why):
        self.negative.setdefault((label, name), []).append((float(weight), why))

    def score(self, label, name):
        positive = _combine(w for w, _ in self.positive.get((label, name), []))
        negative = _combine(w for w, _ in self.negative.get((label, name), []))
        return max(0.0, positive * (1.0 - negative))

    def reasons(self, label, name):
        return [why for _, why in self.positive.get((label, name), [])]

    def names_for(self, label):
        return sorted({name for (lab, name) in self.positive if lab == label})


def similar_name(left, right, minimum=0.75):
    """Whisper spelling drift: "Mahmood" ~ "Mahmoud", "Rafi" !~ "Ravi"."""
    from difflib import SequenceMatcher

    left, right = clean_name(left).casefold(), clean_name(right).casefold()
    if not left or not right or left[0] != right[0]:
        return False
    return left == right or SequenceMatcher(None, left, right).ratio() >= minimum


def _canonical(name, candidates):
    """Map "Alex" to "Alex Rivera" when the roster has the full name,
    and a Whisper misspelling to the listed spelling."""
    name = clean_name(name)
    for candidate in candidates:
        if candidate.casefold() == name.casefold():
            return candidate
    matches = [c for c in candidates if first_name(c).casefold() == name.casefold()]
    if len(matches) == 1:
        return matches[0]
    fuzzy = [c for c in candidates if similar_name(c, name) or similar_name(first_name(c), name)]
    if len(fuzzy) == 1:
        return fuzzy[0]
    return name


def _normalise_spellings(turns, candidates):
    """Copy of `turns` with capitalised words that are near-misses of a known
    first name rewritten to it, for evidence matching only."""
    firsts = {}
    for candidate in candidates:
        first = first_name(candidate)
        if first:
            firsts[first] = candidate
    if not firsts:
        return turns
    fixed = []
    for turn in turns or []:
        text = clean_name(turn.get("text"))

        def swap(match):
            word = match.group(0)
            if word in firsts:
                return word
            hits = [first for first in firsts if similar_name(first, word)]
            return hits[0] if len(hits) == 1 else word

        fixed.append(dict(turn, text=re.sub(r"\b[A-Z][a-z]{2,}\b", swap, text)))
    return fixed


def resolve_speakers(
    turns,
    self_label,
    roster=(),
    self_aliases=(),
    known_people=(),
    voice_matches=None,
    roster_complete=False,
):
    """Return `(speaker_map, renamed_turns)`.

    `turns`: [{start, end, speaker, text, track}] after alignment.
    `roster`: roster entries (see `roster_entry`).
    `voice_matches`: {label: [(name, similarity)]} from the profile store.
    `speaker_map`: {label: {name, display, confidence, status, evidence}}.
    """
    self_label = clean_name(self_label) or "You"
    aliases = {clean_name(a).casefold() for a in list(self_aliases) + [self_label] if clean_name(a)}
    aliases |= {first_name(a).casefold() for a in list(self_aliases) + [self_label] if first_name(a)}
    roster = [entry for entry in (roster or []) if entry.get("name") and entry["name"].casefold() not in aliases]
    roster_names = [entry["name"] for entry in roster]
    strong_roster = [entry["name"] for entry in roster if entry.get("strength", 1.0) >= 0.9]
    known = [clean_name(n) for n in known_people or [] if clean_name(n) and clean_name(n).casefold() not in aliases]
    voice_matches = voice_matches or {}

    remote_labels = []
    speaking = {}
    for turn in turns or []:
        label = clean_name(turn.get("speaker"))
        if not label or label == self_label or turn.get("track") == "self":
            continue
        if label not in remote_labels:
            remote_labels.append(label)
        speaking[label] = speaking.get(label, 0.0) + max(0.0, float(turn.get("end", 0)) - float(turn.get("start", 0)))

    evidence = Evidence()
    preset = {}
    for label in remote_labels:
        if not is_generic_label(label):
            # Already named by Meet captions/tiles upstream.
            preset[label] = label
            evidence.add(label, label, 0.9, "named by Google Meet captions/tiles")

    # Self-introductions on the voice's own lines.
    intro_names = []
    for turn in turns or []:
        label = clean_name(turn.get("speaker"))
        if label not in remote_labels:
            continue
        for name in self_introductions(turn.get("text")):
            if name.casefold() in aliases or first_name(name).casefold() in aliases:
                continue
            canonical = _canonical(name, roster_names + known)
            listed = canonical in roster_names or canonical in known
            weight = 0.95 if listed else 0.8
            where = "attendee" if listed else "not on the attendee list"
            evidence.add(label, canonical, weight, f'introduced themselves ("{name}", {where}) at {_stamp(turn)}')
            if canonical not in intro_names:
                intro_names.append(canonical)

    candidates = []
    for name in roster_names + known + intro_names + [n for matches in voice_matches.values() for n, _ in matches]:
        if name and name not in candidates:
            candidates.append(name)
    # Names people were addressed by are evidence too (they were said), but
    # only through the addressing rule below, so one mention never confirms.
    for turn in turns or []:
        for name in vocative_names(turn.get("text")):
            canonical = _canonical(name, candidates)
            if canonical.casefold() in aliases or first_name(canonical).casefold() in aliases:
                continue
            if canonical not in candidates:
                candidates.append(canonical)

    # Addressing by name (spelling drift normalised to the candidate names).
    ordered = [t for t in _normalise_spellings(turns, candidates) if clean_name(t.get("text"))]
    for index, turn in enumerate(ordered):
        speaker = clean_name(turn.get("speaker"))
        for name in candidates:
            positions = _vocative_positions(turn.get("text"), name)
            if not positions:
                continue
            if speaker in remote_labels:
                evidence.deny(speaker, name, 0.6, f"addressed {name} by name")
            who = "you" if speaker == self_label else speaker
            if "start" in positions:
                target = _neighbour(ordered, index, -1, speaker, self_label)
                if target:
                    evidence.add(target, name, 0.4, f'{who} answered them as "{name}" at {_stamp(turn)}')
            if "end" in positions:
                target = _neighbour(ordered, index, +1, speaker, self_label)
                if target:
                    evidence.add(target, name, 0.4, f'{who} asked "{name}" and they answered at {_stamp(turn)}')
            if "middle" in positions:
                for step in (-1, +1):
                    target = _neighbour(ordered, index, step, speaker, self_label)
                    if target:
                        evidence.add(target, name, 0.15, f'{who} mentioned "{name}" near their turn')

    # Voice profiles from earlier confirmed calls.
    for label, matches in voice_matches.items():
        if label not in remote_labels:
            continue
        for name, similarity in matches:
            if similarity >= 0.70:
                weight = min(0.95, 0.75 + (similarity - 0.70) * 1.4)
                evidence.add(label, name, weight, f"voice matches the saved profile of {name} ({similarity:.2f})")
            elif similarity >= 0.55:
                evidence.add(label, name, 0.4, f"voice is similar to the saved profile of {name} ({similarity:.2f})")

    # Roster priors.
    if len(remote_labels) == 1 and len(roster_names) == 1:
        entry = roster[0]
        weight = 0.9 if entry.get("strength", 1.0) >= 0.9 else 0.5
        evidence.add(remote_labels[0], entry["name"], weight, f"the only other attendee ({entry['source']})")
    elif roster_names and remote_labels:
        dominant = max(remote_labels, key=lambda lab: speaking.get(lab, 0.0))
        for entry in roster:
            if len(roster_names) == 1:
                evidence.add(dominant, entry["name"], 0.3, f"the only listed attendee ({entry['source']}); this voice spoke most")

    # Greedy assignment by score, one name per voice.
    pairs = []
    for label in remote_labels:
        for name in evidence.names_for(label):
            pairs.append((evidence.score(label, name), label, name))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))
    assigned = {}
    used = set()
    for score, label, name in pairs:
        if score < NAME_THRESHOLD or label in assigned or name.casefold() in used:
            continue
        assigned[label] = (name, score)
        used.add(name.casefold())

    # Elimination: one voice and one attendee left over.
    leftover_labels = [lab for lab in remote_labels if lab not in assigned]
    leftover_names = [n for n in (strong_roster if roster_complete or strong_roster else []) if n.casefold() not in used]
    if len(leftover_labels) == 1 and len(leftover_names) == 1 and len(remote_labels) > 1 and roster_complete:
        label, name = leftover_labels[0], leftover_names[0]
        if evidence.score(label, name) >= 0 and not evidence.negative.get((label, name)):
            evidence.add(label, name, 0.75, "the only attendee left after the other voices were named")
            assigned[label] = (name, evidence.score(label, name))
            used.add(name.casefold())

    speaker_map = {}
    number_used = {int(lab.split()[1]) for lab in remote_labels if re.match(r"^Speaker \d+$", lab)}
    next_number = 2
    for label in remote_labels:
        if label in assigned:
            name, score = assigned[label]
            speaker_map[label] = {
                "name": name,
                "display": name,
                "confidence": round(score, 2),
                "status": "confirmed",
                "evidence": evidence.reasons(label, name),
            }
            continue
        base = label
        if not re.match(r"^Speaker \d+$", label):
            while next_number in number_used:
                next_number += 1
            base = f"Speaker {next_number}"
            number_used.add(next_number)
        hint = None
        for score, lab, name in pairs:
            if lab == label and name.casefold() not in used and score >= HINT_THRESHOLD:
                hint = (name, score)
                break
        if hint:
            speaker_map[label] = {
                "name": None,
                "display": f"{base} (unconfirmed: likely {hint[0]})",
                "confidence": round(hint[1], 2),
                "status": "unconfirmed",
                "likely": hint[0],
                "evidence": evidence.reasons(label, hint[0]),
            }
        else:
            speaker_map[label] = {
                "name": None,
                "display": base,
                "confidence": 0.0,
                "status": "unknown",
                "evidence": [],
            }

    renamed = []
    for turn in turns or []:
        label = clean_name(turn.get("speaker"))
        item = dict(turn)
        if label in speaker_map:
            item["speaker"] = speaker_map[label]["display"]
            item["voice"] = label
        renamed.append(item)
    speaker_map_self = {
        "name": self_label,
        "display": self_label,
        "confidence": 1.0,
        "status": "confirmed",
        "evidence": ["microphone track"],
    }
    return dict({self_label: speaker_map_self}, **speaker_map), renamed


def _neighbour(turns, index, step, speaker, self_label):
    """The nearest turn in direction `step` by someone other than `speaker`."""
    position = index + step
    hops = 0
    while 0 <= position < len(turns) and hops < 2:
        other = clean_name(turns[position].get("speaker"))
        if other and other != speaker:
            if other == self_label or turns[position].get("track") == "self":
                return None
            return other
        position += step
        hops += 1
    return None


def _stamp(turn):
    seconds = max(0.0, float(turn.get("start", 0.0)))
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"
