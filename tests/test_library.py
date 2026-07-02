import os

from meeting_notes.library import Meeting, list_meetings, load_notes


def _make_meeting(base, folder, notes=None):
    d = base / folder
    d.mkdir(parents=True)
    if notes is not None:
        (d / "notes.md").write_text(notes, encoding="utf-8")


def test_list_meetings_empty_when_dir_missing(tmp_path):
    assert list_meetings(str(tmp_path / "nope")) == []


def test_list_meetings_parses_and_sorts_desc(tmp_path):
    _make_meeting(tmp_path, "2026-06-30_client-kickoff", notes="x")
    _make_meeting(tmp_path, "2026-07-01_standup")
    (tmp_path / "stray-file.txt").write_text("ignore me")

    meetings = list_meetings(str(tmp_path))

    assert [m.folder for m in meetings] == [
        "2026-07-01_standup",
        "2026-06-30_client-kickoff",
    ]
    first = meetings[0]
    assert first.date == "2026-07-01"
    assert first.title == "Standup"
    assert first.has_notes is False
    second = meetings[1]
    assert second.title == "Client Kickoff"
    assert second.has_notes is True
    assert second.notes_path == os.path.join(str(tmp_path), second.folder, "notes.md")


def test_list_meetings_handles_folder_without_underscore(tmp_path):
    _make_meeting(tmp_path, "randomfolder")
    (m,) = list_meetings(str(tmp_path))
    assert m.title == "randomfolder"


def test_load_notes_reads_utf8(tmp_path):
    _make_meeting(tmp_path, "2026-07-01_sync", notes="**Summary**\n- ümlaut")
    (m,) = list_meetings(str(tmp_path))
    assert load_notes(m) == "**Summary**\n- ümlaut"
