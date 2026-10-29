from datetime import datetime, timezone

from meeting_notes.session import allocate_meeting_dir, has_transcript, slugify


def test_slugify_matches_historical_rules():
    assert slugify("Weekly Standup") == "weekly-standup"
    assert slugify("  Q3 / Planning! ") == "q3-planning"


def test_manual_folder_reuses_name_and_automated_adds_time_suffix(tmp_path):
    when = datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc)
    manual = allocate_meeting_dir(str(tmp_path), "Weekly Standup", unique=False, when=when)
    assert os_name(manual) == "2026-10-29_weekly-standup"

    again = allocate_meeting_dir(str(tmp_path), "Weekly Standup", unique=False, when=when)
    assert again == manual

    first_auto = allocate_meeting_dir(str(tmp_path), "Weekly Standup", unique=True, when=when)
    assert os_name(first_auto) == "2026-10-29_weekly-standup_1430"

    second_auto = allocate_meeting_dir(str(tmp_path), "Weekly Standup", unique=True, when=when)
    assert os_name(second_auto) == "2026-10-29_weekly-standup_1430_2"


def test_unique_folder_when_primary_is_free_has_no_time_suffix(tmp_path):
    when = datetime(2026, 10, 29, 9, 5, tzinfo=timezone.utc)
    path = allocate_meeting_dir(str(tmp_path), "Standup", unique=True, when=when)
    assert os_name(path) == "2026-10-29_standup"


def test_has_transcript_is_true_for_any_existing_file(tmp_path):
    assert has_transcript(str(tmp_path)) is False
    (tmp_path / "transcript.txt").write_text("", encoding="utf-8")
    assert has_transcript(str(tmp_path)) is True
    (tmp_path / "transcript.txt").write_text("[00:00] hi", encoding="utf-8")
    assert has_transcript(str(tmp_path)) is True


def os_name(path):
    import os

    return os.path.basename(path)
