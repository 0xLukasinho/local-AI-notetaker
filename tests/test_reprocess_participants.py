"""`reprocess --participants` feeds post-call names into speaker naming."""

import json
from types import SimpleNamespace

from meeting_notes import cli


def test_reprocess_participants_reach_status_before_transcription(tmp_path, monkeypatch):
    folder = tmp_path / "2026-01-02_abc-defg-hij"
    folder.mkdir()
    (folder / "audio.wav").write_bytes(b"RIFF")
    (folder / "status.json").write_text(json.dumps({"meet_participants": ["Alex Rivera"]}), encoding="utf-8")

    seen = {}

    def fake_transcript(meeting_dir, config, whisper_model, language):
        with open(f"{meeting_dir}/status.json", encoding="utf-8") as handle:
            seen["status"] = json.load(handle)
        return "text"

    monkeypatch.setattr(cli, "_resolve_folder", lambda name: str(folder))
    monkeypatch.setattr(cli, "_write_transcript", fake_transcript)
    monkeypatch.setattr(cli, "_maybe_delete_audio", lambda *a, **k: None)
    args = SimpleNamespace(
        folder=folder.name,
        transcript_only=True,
        no_summary=False,
        notes_only=False,
        model=None,
        whisper_model=None,
        language=None,
        keep_audio=True,
        participants="Priya Shah, alex rivera,  Jordan Lee",
    )
    cli.cmd_reprocess(args)
    assert seen["status"]["meet_participants"] == ["Alex Rivera", "Priya Shah", "Jordan Lee"]
