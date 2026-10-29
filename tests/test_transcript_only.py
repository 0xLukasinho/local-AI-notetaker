import os
import subprocess
import sys
import threading

import pytest

import meeting_notes.cli as cli
from meeting_notes.config import Config
from meeting_notes.session import read_active, read_status, write_active


class _FixedDate(cli.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 29)


def _dirs(monkeypatch, tmp_path):
    meetings = tmp_path / "meetings"
    control = tmp_path / "control"
    meetings.mkdir()
    control.mkdir()
    monkeypatch.setattr(cli, "OUTPUT_DIR", str(meetings))
    monkeypatch.setattr(cli, "CONTROL_DIR", str(control))
    monkeypatch.setattr(cli, "date", _FixedDate)
    return meetings, control


def _pipeline(monkeypatch, order=None):
    order = [] if order is None else order

    def record(path):
        order.append("record")
        with open(path, "wb") as handle:
            handle.write(b"RIFFaudio")
        stop = os.path.join(os.path.dirname(path), "STOP")
        if os.environ.get("MEETING_NOTES_TEST_WRITE_STOP") == "1":
            with open(stop, "w", encoding="utf-8") as handle:
                handle.write("stop\n")
        return {"path": path}

    def stop(_handle):
        order.append("stop")

    def transcribe(_path, model_size="medium", language="en"):
        order.append("transcribe")
        return "[00:03] transcript line"

    def generate(*_args, **_kwargs):
        order.append("notes")
        raise AssertionError("summarizer must not run in transcript-only mode")

    def install():
        if os.environ.get("MEETING_NOTES_TEST_WRITE_STOP") == "1":
            return threading.Event()
        event = threading.Event()
        event.set()
        return event

    monkeypatch.setattr(cli, "_install_stop_signal", install)
    monkeypatch.setattr(cli, "record_audio", record)
    monkeypatch.setattr(cli, "stop_recording", stop)
    monkeypatch.setattr(cli, "transcribe", transcribe)
    monkeypatch.setattr(cli, "generate_notes", generate)
    return order


def test_transcript_only_skips_claude_and_writes_no_notes(tmp_path, monkeypatch, capsys):
    meetings, control = _dirs(monkeypatch, tmp_path)
    order = _pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["start", "Weekly Standup", "--transcript-only"])
    cli.cmd_start(args)
    out = capsys.readouterr().out
    folder = meetings / "2026-10-29_weekly-standup"
    assert "Transcript-only mode: skipping note generation." in out
    assert "Note Generation" not in out
    assert "notes" not in order
    assert not (folder / "notes.md").exists()
    assert (folder / "audio.wav").is_file()
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "[00:03] transcript line"
    status = read_status(str(folder))
    assert status["state"] == "done"
    assert status["transcript_only"] is True
    assert status["mode"] == "manual"
    assert read_active(str(control)) is None


def test_no_summary_alias_and_config_option(tmp_path, monkeypatch):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    _pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["start", "Weekly Standup", "--no-summary"])
    cli.cmd_start(args)
    assert not (meetings / "2026-10-29_weekly-standup" / "notes.md").exists()

    monkeypatch.setattr(cli, "load_config", lambda control_dir=None: Config(transcript_only=True))
    args = cli.build_parser().parse_args(["start", "Design Review"])
    cli.cmd_start(args)
    assert not (meetings / "2026-10-29_design-review" / "notes.md").exists()
    assert read_status(str(meetings / "2026-10-29_design-review"))["transcript_only"] is True


def test_manual_start_does_not_overwrite_an_existing_transcript(tmp_path, monkeypatch, capsys):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    folder = meetings / "2026-10-29_weekly-standup"
    folder.mkdir()
    (folder / "transcript.txt").write_text("[00:00] original", encoding="utf-8")
    order = _pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["start", "Weekly Standup"])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_start(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "will not be overwritten" in err
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "[00:00] original"
    assert order == []


def test_stop_file_flushes_before_transcription(tmp_path, monkeypatch, capsys):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    monkeypatch.setenv("MEETING_NOTES_TEST_WRITE_STOP", "1")
    order = _pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["start", "Weekly Standup", "--transcript-only"])
    cli.cmd_start(args)
    assert order == ["record", "stop", "transcribe"]
    out = capsys.readouterr().out
    assert "Stopping recording..." in out
    assert out.index("Stopping recording...") < out.index("--- Transcription ---")
    folder = meetings / "2026-10-29_weekly-standup"
    assert (folder / "transcript.txt").is_file()


def test_automated_folder_gets_a_time_suffix_when_the_name_is_taken(tmp_path, monkeypatch):
    from datetime import datetime

    import meeting_notes.session as session

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 29, 14, 30)

    meetings, _control = _dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(session, "datetime", _FixedDateTime)
    _pipeline(monkeypatch)

    def run(extra):
        cli.cmd_start(cli.build_parser().parse_args(["start", "Weekly Standup", *extra]))

    run(["--automated", "--transcript-only"])
    run(["--automated", "--transcript-only"])
    first = meetings / "2026-10-29_weekly-standup"
    second = meetings / "2026-10-29_weekly-standup_1430"
    assert first.is_dir() and second.is_dir()
    assert read_status(str(second))["mode"] == "watch"
    assert (first / "transcript.txt").is_file()
    assert (second / "transcript.txt").is_file()

    with pytest.raises(SystemExit):
        run([])
    assert (first / "transcript.txt").read_text(encoding="utf-8") == "[00:03] transcript line"


def test_human_start_warns_and_refuses_during_a_watcher_session(tmp_path, monkeypatch, capsys):
    _meetings, control = _dirs(monkeypatch, tmp_path)
    order = _pipeline(monkeypatch)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        write_active(
            str(control),
            {
                "pid": proc.pid,
                "folder": str(tmp_path / "watcher-session"),
                "mode": "watch",
                "break_ok": False,
            },
        )
        args = cli.build_parser().parse_args(["start", "Manual Notes"])
        with pytest.raises(SystemExit) as exc:
            cli.cmd_start(args)
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "watcher recording is already in progress" in err
    assert "meeting-notes stop" in err
    assert order == []


def test_automated_start_refuses_a_second_session(tmp_path, monkeypatch, capsys):
    _meetings, control = _dirs(monkeypatch, tmp_path)
    _pipeline(monkeypatch)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        write_active(
            str(control),
            {
                "pid": proc.pid,
                "folder": str(tmp_path / "human-session"),
                "mode": "manual",
            },
        )
        args = cli.build_parser().parse_args(["start", "abc-defg-hij", "--automated", "--transcript-only"])
        with pytest.raises(SystemExit) as exc:
            cli.cmd_start(args)
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already running" in err
    assert "mode manual" in err


def test_reprocess_transcript_only_skips_notes_and_keeps_audio(tmp_path, monkeypatch, capsys):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    folder = meetings / "2026-10-29_weekly-standup"
    folder.mkdir()
    (folder / "audio.wav").write_bytes(b"RIFFaudio")
    (folder / "notes.md").write_text("old notes", encoding="utf-8")
    order = _pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["reprocess", folder.name, "--transcript-only"])
    cli.cmd_reprocess(args)
    out = capsys.readouterr().out
    assert "Transcript-only mode: skipping note generation." in out
    assert "notes" not in order
    assert (folder / "notes.md").read_text(encoding="utf-8") == "old notes"
    assert (folder / "audio.wav").is_file()
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "[00:03] transcript line"


def test_reprocess_rejects_transcript_only_with_notes_only(tmp_path, monkeypatch, capsys):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    folder = meetings / "2026-10-29_weekly-standup"
    folder.mkdir()
    args = cli.build_parser().parse_args(
        ["reprocess", folder.name, "--notes-only", "--no-summary"]
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_reprocess(args)
    assert exc.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err
