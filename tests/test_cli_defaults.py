import os
import sys
import threading
from datetime import date

import pytest

import meeting_notes.cli as cli
import meeting_notes.summarizer as summarizer
from meeting_notes.session import read_status


class _FixedDate(date):
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


def _stop_immediately():
    event = threading.Event()
    event.set()
    return event


def _patch_pipeline(monkeypatch, transcript="[00:01] hello", notes="**Summary**\n- shipped"):
    calls = {"record": 0, "stop": 0, "transcribe": 0, "notes": []}

    def record(path):
        calls["record"] += 1
        with open(path, "wb") as handle:
            handle.write(b"RIFFaudio")
        return {"path": path}

    def stop(_handle):
        calls["stop"] += 1

    def transcribe(_path, model_size="medium", language="en"):
        calls["transcribe"] += 1
        calls["whisper"] = (model_size, language)
        return transcript

    def generate(text, meeting_name, meeting_date, model=None):
        calls["notes"].append((text, meeting_name, meeting_date, model))
        return notes

    monkeypatch.setattr(cli, "_install_stop_signal", _stop_immediately)
    monkeypatch.setattr(cli, "record_audio", record)
    monkeypatch.setattr(cli, "stop_recording", stop)
    monkeypatch.setattr(cli, "transcribe", transcribe)
    monkeypatch.setattr(cli, "generate_notes", generate)
    return calls


def test_parser_defaults_match_the_historical_commands():
    parser = cli.build_parser()
    start = parser.parse_args(["start", "Weekly Standup"])
    assert start.command == "start"
    assert start.name == "Weekly Standup"
    assert start.model is None
    assert start.whisper_model is None
    assert start.language is None
    assert start.keep_audio is False
    assert start.transcript_only is False
    assert start.no_summary is False
    assert start.automated is False
    assert start.output_dir is None

    reprocess = parser.parse_args(["reprocess", "2026-02-19_weekly-standup"])
    assert reprocess.notes_only is False
    assert reprocess.keep_audio is False
    assert reprocess.transcript_only is False
    assert reprocess.no_summary is False

    listed = parser.parse_args(["list"])
    assert listed.command == "list"
    assert listed.func == cli.cmd_list

    help_text = parser.format_help()
    for name in ("start", "reprocess", "list", "stop", "deliver", "watch"):
        assert name in help_text


def test_default_start_still_summarizes_and_uses_the_plain_folder_name(tmp_path, monkeypatch, capsys):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    calls = _patch_pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["start", "Weekly Standup"])
    cli.cmd_start(args)

    out = capsys.readouterr().out
    folder = meetings / "2026-10-29_weekly-standup"
    assert folder.is_dir()
    assert "Recording meeting: Weekly Standup" in out
    assert f"Output directory: {folder}" in out
    assert "\n--- Transcription ---" in out
    assert "Transcript saved:" in out
    assert "\n--- Note Generation ---" in out
    assert "Notes saved:" in out
    assert "Done!" in out
    assert "Transcript-only" not in out
    assert "status.json" not in out

    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "[00:01] hello"
    assert (folder / "notes.md").read_text(encoding="utf-8") == "**Summary**\n- shipped"
    assert not (folder / "audio.wav").exists()
    assert calls["notes"] == [
        ("[00:01] hello", "Weekly Standup", "2026-10-29", cli.DEFAULT_MODEL)
    ]
    assert calls["record"] == 1 and calls["stop"] == 1 and calls["transcribe"] == 1
    status = read_status(str(folder))
    assert status["state"] == "done"
    assert status["mode"] == "manual"
    assert status["transcript_only"] is False
    assert status["error"] is None
    assert status["pid"] == os.getpid()


def test_keep_audio_still_retains_the_wav(tmp_path, monkeypatch):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    _patch_pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["start", "Weekly Standup", "--keep-audio"])
    cli.cmd_start(args)
    assert (meetings / "2026-10-29_weekly-standup" / "audio.wav").is_file()


def test_default_reprocess_still_transcribes_and_summarizes(tmp_path, monkeypatch, capsys):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    folder = meetings / "2026-10-29_weekly-standup"
    folder.mkdir()
    (folder / "audio.wav").write_bytes(b"RIFFaudio")
    calls = _patch_pipeline(monkeypatch)
    args = cli.build_parser().parse_args(["reprocess", folder.name])
    cli.cmd_reprocess(args)
    out = capsys.readouterr().out
    assert "Reprocessing: Weekly Standup" in out
    assert "\n--- Transcription ---" in out
    assert "\n--- Note Generation ---" in out
    assert "Notes saved:" in out
    assert "Transcript-only" not in out
    assert (folder / "transcript.txt").read_text(encoding="utf-8") == "[00:01] hello"
    assert (folder / "notes.md").is_file()
    assert not (folder / "audio.wav").exists()
    assert len(calls["notes"]) == 1


def test_notes_only_still_summarizes_when_config_transcript_only_is_set(tmp_path, monkeypatch):
    meetings, _control = _dirs(monkeypatch, tmp_path)
    folder = meetings / "2026-10-29_weekly-standup"
    folder.mkdir()
    (folder / "transcript.txt").write_text("[00:01] hello", encoding="utf-8")
    (folder / "audio.wav").write_bytes(b"RIFFaudio")
    calls = _patch_pipeline(monkeypatch)

    from meeting_notes.config import Config

    monkeypatch.setattr(cli, "load_config", lambda control_dir=None: Config(transcript_only=True))
    args = cli.build_parser().parse_args(["reprocess", folder.name, "--notes-only"])
    cli.cmd_reprocess(args)
    assert len(calls["notes"]) == 1
    assert calls["transcribe"] == 0
    assert (folder / "notes.md").is_file()


def test_no_audio_error_text_is_unchanged(tmp_path, monkeypatch, capsys):
    _dirs(monkeypatch, tmp_path)

    def record(path):
        with open(path, "wb") as handle:
            handle.write(b"")
        return {}

    monkeypatch.setattr(cli, "_install_stop_signal", _stop_immediately)
    monkeypatch.setattr(cli, "record_audio", record)
    monkeypatch.setattr(cli, "stop_recording", lambda _handle: None)
    args = cli.build_parser().parse_args(["start", "Weekly Standup"])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_start(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Error: No audio was recorded. Check your audio device settings." in err


def test_summarizer_still_invokes_claude_p(monkeypatch):
    seen = {}

    monkeypatch.setattr(summarizer.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["input"] = kwargs.get("input")
        return subprocess_result(args)

    monkeypatch.setattr(summarizer.subprocess, "run", fake_run)
    notes = summarizer.generate_notes("[00:01] hello", "Standup", "2026-10-29", model="claude-opus-4-7")
    assert notes
    assert seen["args"][0] == "/usr/bin/claude"
    assert seen["args"][1] == "-p"
    assert "--model" in seen["args"]
    assert "claude-opus-4-7" in seen["args"]
    assert "[00:01] hello" in seen["input"]


def subprocess_result(args):
    import subprocess

    return subprocess.CompletedProcess(args, 0, stdout="**Summary**\n- ok\n", stderr="")


def test_recorder_imports_on_linux_and_refuses_to_record():
    import meeting_notes.recorder as recorder

    if sys.platform in ("darwin", "win32"):
        return
    with pytest.raises(RuntimeError):
        recorder.record_audio("unused.wav")
