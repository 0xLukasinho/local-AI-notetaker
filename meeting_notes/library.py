"""Meeting library data layer: scan ~/meeting-notes and load notes.

Pure filesystem code — no Qt — so it is unit-testable and shared between the
desktop app and the CLI's `list` command.
"""

import os
from dataclasses import dataclass

DEFAULT_BASE_DIR = os.path.expanduser("~/meeting-notes")


@dataclass(frozen=True)
class Meeting:
    folder: str      # e.g. "2026-07-01_standup"
    path: str        # absolute dir path
    date: str        # "2026-07-01" (from folder name)
    title: str       # "Standup" (from folder name)
    notes_path: str  # absolute path to notes.md (may not exist)
    has_notes: bool


def _parse_folder_name(folder):
    parts = folder.split("_", 1)
    date = parts[0] if parts else "unknown"
    title = parts[1].replace("-", " ").title() if len(parts) > 1 else folder
    return date, title


def list_meetings(base_dir=DEFAULT_BASE_DIR):
    """Return all meeting folders, most recent first (name-sorted desc)."""
    if not os.path.isdir(base_dir):
        return []
    meetings = []
    for entry in sorted(os.listdir(base_dir), reverse=True):
        path = os.path.join(base_dir, entry)
        if not os.path.isdir(path):
            continue
        date, title = _parse_folder_name(entry)
        notes_path = os.path.join(path, "notes.md")
        meetings.append(
            Meeting(
                folder=entry,
                path=path,
                date=date,
                title=title,
                notes_path=notes_path,
                has_notes=os.path.exists(notes_path),
            )
        )
    return meetings


def load_notes(meeting):
    """Return the raw markdown of a meeting's notes.md."""
    with open(meeting.notes_path, "r", encoding="utf-8") as f:
        return f.read()
