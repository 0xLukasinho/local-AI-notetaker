"""Local voice profiles for people who come back (Mahmoud, Alex, partners).

One JSON file, `~/.meeting-notes/voices/profiles.json`:
`{"version": 1, "people": {"Alex Rivera": {"embedding": [...], "count": 3,
"updated_at": "...", "sessions": ["2026-11-25_abc-defg-hij"]}}}`.

An embedding is a 256-float WeSpeaker vector, never audio. A profile is only
created or updated from a voice whose name was confirmed (confidence >=
UPDATE_THRESHOLD) or that the user named with `meeting-notes speakers`.
"""

import json
import math
import os
import time

UPDATE_THRESHOLD = 0.85
MATCH_FLOOR = 0.55
VERSION = 1


def default_store_path():
    return os.path.join(os.path.expanduser("~/.meeting-notes"), "voices", "profiles.json")


def _normalize(vector):
    norm = math.sqrt(sum(float(v) * float(v) for v in vector))
    if norm <= 0:
        return []
    return [float(v) / norm for v in vector]


def _cosine(left, right):
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


class VoiceProfiles:
    def __init__(self, path=None):
        self.path = path or default_store_path()
        self.people = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        people = data.get("people") if isinstance(data, dict) else None
        if isinstance(people, dict):
            for name, item in people.items():
                if isinstance(item, dict) and item.get("embedding"):
                    self.people[name] = item

    def save(self):
        from meeting_notes.atomic_io import atomic_write_json

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        atomic_write_json(self.path, {"version": VERSION, "people": self.people})
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def names(self):
        return sorted(self.people)

    def match(self, embedding, floor=MATCH_FLOOR):
        """[(name, similarity)] best first, only above `floor`."""
        vector = _normalize(embedding or [])
        found = []
        for name, item in self.people.items():
            similarity = _cosine(vector, item.get("embedding") or [])
            if similarity >= floor:
                found.append((name, round(similarity, 3)))
        found.sort(key=lambda pair: -pair[1])
        return found

    def update(self, name, embedding, session=""):
        vector = _normalize(embedding or [])
        if not name or not vector:
            return False
        item = self.people.get(name)
        if item is None or len(item.get("embedding") or []) != len(vector):
            item = {"embedding": vector, "count": 1, "sessions": []}
        else:
            count = int(item.get("count") or 1)
            weight = min(count, 9)  # recent calls still move the profile
            merged = [(old * weight + new) / (weight + 1) for old, new in zip(item["embedding"], vector)]
            item["embedding"] = _normalize(merged)
            item["count"] = count + 1
        sessions = [s for s in item.get("sessions") or [] if s != session]
        if session:
            sessions.append(session)
        item["sessions"] = sessions[-20:]
        item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.people[name] = item
        return True

    def forget(self, name):
        return self.people.pop(name, None) is not None
