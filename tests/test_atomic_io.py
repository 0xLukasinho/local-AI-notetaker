import json
import os

import pytest

from meeting_notes.atomic_io import atomic_write_json, atomic_write_text
from meeting_notes.session import read_status, update_status, write_status


def test_atomic_write_text_is_complete_and_leaves_no_temp(tmp_path):
    path = tmp_path / "transcript.txt"
    body = "[00:01] hello\n[00:02] world"
    atomic_write_text(str(path), body)
    assert path.read_text(encoding="utf-8") == body
    assert list(tmp_path.glob(".tmp-*")) == []


def test_atomic_write_does_not_publish_partial_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "transcript.txt"
    path.write_text("previous complete", encoding="utf-8")

    def boom(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(str(path), "a partial new transcript")
    assert path.read_text(encoding="utf-8") == "previous complete"
    assert list(tmp_path.glob(".tmp-*")) == []


def test_atomic_write_json_roundtrip(tmp_path):
    path = tmp_path / "status.json"
    atomic_write_json(str(path), {"state": "recording", "pid": 5})
    assert json.loads(path.read_text(encoding="utf-8")) == {"pid": 5, "state": "recording"}
    assert list(tmp_path.glob(".tmp-*")) == []


def test_status_update_preserves_left_at(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    write_status(
        str(folder),
        {
            "state": "recording",
            "left_at": "2026-10-29T15:00:00+00:00",
            "joined_at": "2026-10-29T14:00:00+00:00",
        },
    )
    update_status(
        str(folder),
        {
            "state": "transcribing",
            "left_at": "2026-10-29T15:05:00+00:00",
            "error": None,
        },
    )
    status = read_status(str(folder))
    assert status["state"] == "transcribing"
    assert status["left_at"] == "2026-10-29T15:00:00+00:00"
    assert status["joined_at"] == "2026-10-29T14:00:00+00:00"
    assert status["error"] is None
    assert "updated_at" in status
