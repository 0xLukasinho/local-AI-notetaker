"""Audio is only deleted after a verified transcript is stored; full archive; acks."""

import hashlib
import json
import os

import pytest

from meeting_notes import deliver, session_finish
from meeting_notes.config import Config


def _session(tmp_path, transcript="[00:00] Lukas: hello", document=None, mode="watch"):
    folder = tmp_path / "2026-11-25_call"
    folder.mkdir()
    for name in ("audio.wav", "audio-self.wav", "audio-others.wav"):
        (folder / name).write_bytes(b"RIFF" + b"\0" * 100)
    (folder / "transcript.txt").write_text(transcript)
    if document is not None:
        (folder / "transcript.json").write_text(json.dumps(document))
    (folder / "status.json").write_text(json.dumps({"mode": mode, "state": "transcribing"}))
    return folder


GOOD = {"segments": [{"start": 0, "end": 1, "speaker": "Lukas", "text": "hello"}], "speaker_source": "named"}
MIC_ONLY = dict(GOOD, speaker_source="mic-only", warning="System audio was not captured")


def _finish(folder, config, delivered):
    return session_finish.finish_folder(
        str(folder), config, transcript_only=True, deliver_fn=lambda path: delivered, log=None
    )


def test_mic_only_transcript_keeps_the_audio(tmp_path):
    folder = _session(tmp_path, document=MIC_ONLY)
    result = _finish(folder, Config(webhook_url="https://x"), delivered=True)
    assert result["deleted_audio"] == []
    assert (folder / "audio-others.wav").exists()
    status = json.loads((folder / "status.json").read_text())
    assert status["audio_kept"] is True and "mic-only" in status["audio_kept_reason"]


def test_undelivered_watch_session_keeps_audio_until_delivered(tmp_path):
    folder = _session(tmp_path, document=GOOD)
    result = _finish(folder, Config(webhook_url="https://x"), delivered=False)
    assert result["deleted_audio"] == []
    assert (folder / "audio.wav").exists()


def test_verified_and_delivered_session_drops_audio(tmp_path):
    folder = _session(tmp_path, document=GOOD)
    result = _finish(folder, Config(webhook_url="https://x"), delivered=True)
    assert len(result["deleted_audio"]) == 3
    assert not (folder / "audio.wav").exists()


def test_archive_counts_as_stored_and_is_checksummed(tmp_path):
    folder = _session(tmp_path, transcript="line one\nline two\n" * 500, document=GOOD)
    archive = tmp_path / "Drive" / "Meeting Transcripts"
    config = Config(webhook_url="https://x", transcript_archive_dir=str(archive))
    result = _finish(folder, config, delivered=False)
    copied = archive / folder.name / "transcript.txt"
    assert copied.read_text() == (folder / "transcript.txt").read_text()
    digest = hashlib.sha256(copied.read_bytes()).hexdigest()
    assert (archive / folder.name / "transcript.sha256").read_text().split()[0] == digest
    assert (archive / folder.name / "transcript.json").exists()
    assert len(result["deleted_audio"]) == 3  # archived + verified is enough
    status = json.loads((folder / "status.json").read_text())
    assert status["archived_to"] == str(copied)


def test_empty_transcript_is_not_resumed_forever(tmp_path):
    folder = _session(tmp_path, transcript="")
    status = {"mode": "watch", "state": "done", "transcript_empty": True, "delivered": True}
    (folder / "status.json").write_text(json.dumps(status))
    assert session_finish.unfinished_folders(str(tmp_path)) == []
    status["transcript_empty"] = False
    (folder / "status.json").write_text(json.dumps(status))
    assert session_finish.unfinished_folders(str(tmp_path)) == [str(folder)]


def test_receiver_checksum_mismatch_is_a_failed_delivery():
    body = {"transcript_sha256": "abc", "chunk_index": 1, "chunk_count": 2}
    deliver.check_receiver_ack(body, json.dumps({"transcript_sha256": "abc"}).encode())
    deliver.check_receiver_ack(body, b"ok")
    deliver.check_receiver_ack(dict(body, chunk_index=0), json.dumps({"sha256": "zzz"}).encode())  # not last chunk
    with pytest.raises(deliver.DeliveryError):
        deliver.check_receiver_ack(body, json.dumps({"sha256": "zzz"}).encode())


def test_chunks_reassemble_to_the_exact_transcript():
    text = "".join(f"[{i // 60:02d}:{i % 60:02d}] Speaker {i % 3 + 2}: sentence number {i} with some words.\n" for i in range(900))
    payload = {"folder_name": "x", "transcript": text, "speakers": []}
    bodies = list(deliver.iter_delivery_bodies(payload, 6000))
    assert len(bodies) > 5
    assert all(len(json.dumps(body, ensure_ascii=False).encode()) <= 6000 for body in bodies)
    rebuilt = "".join(body["transcript"] for body in sorted(bodies, key=lambda b: b["chunk_index"]))
    assert hashlib.sha256(rebuilt.encode()).hexdigest() == bodies[0]["transcript_sha256"]
    assert {body["chunk_count"] for body in bodies} == {len(bodies)}


def test_unconfirmed_speaker_keeps_audio_and_flags_relabel(tmp_path):
    import json as _json
    from types import SimpleNamespace

    from meeting_notes import session_finish

    folder = tmp_path / "2026-01-02_abc-defg-hij"
    folder.mkdir()
    (folder / "audio.wav").write_bytes(b"RIFF" + b"\0" * 64)
    (folder / "transcript.txt").write_text("[00:01] Lukas: hi\n[00:02] Speaker 2: hello\n", encoding="utf-8")
    (folder / "status.json").write_text(
        _json.dumps(
            {
                "speaker_map": {
                    "Lukas": {"status": "confirmed", "display": "Lukas"},
                    "Speaker 2": {"status": "unconfirmed", "display": "Speaker 2 (unconfirmed: likely Jordan Lee)"},
                }
            }
        ),
        encoding="utf-8",
    )
    config = SimpleNamespace(delete_audio_requires_delivery=True)
    removed = session_finish._retire_audio(str(folder), config, False, True, True, True, lambda *a, **k: None)
    assert removed == []
    assert (folder / "audio.wav").exists()
    status = _json.loads((folder / "status.json").read_text(encoding="utf-8"))
    assert status["needs_relabel"] is True
    assert status["audio_kept"] is True
    assert "not confident" in status["audio_kept_reason"]
