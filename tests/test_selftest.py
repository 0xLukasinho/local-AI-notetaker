import io
import wave

from meeting_notes import selftest


def _wav(path, seconds, value):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(int(value).to_bytes(2, "little", signed=True) * int(seconds * 16000))


def test_wav_stats(tmp_path):
    _wav(tmp_path / "a.wav", 2, 1000)
    seconds, rms, nonzero = selftest.wav_stats(str(tmp_path / "a.wav"))
    assert seconds == 2.0 and round(rms) == 1000 and nonzero == 1.0


def _result(seconds, wall, rms=500.0, nonzero=0.5):
    return {"wall_seconds": wall, "tracks": {"audio-others.wav": {"seconds": seconds, "rms": rms, "nonzero": nonzero, "ratio": seconds / wall}}}


def test_recorder_length_must_match_wall_clock():
    report = selftest.Report(io.StringIO())
    assert selftest.judge_capture(_result(62.5, 65.4), report)
    report = selftest.Report(io.StringIO())
    # The Nov 24 failure mode: Background throttling lost half the audio.
    assert not selftest.judge_capture(_result(31.0, 65.0), report)
    assert any(name == "length" and status == "FAIL" for status, name, _ in report.items)


def test_silent_remote_track_fails():
    report = selftest.Report(io.StringIO())
    assert not selftest.judge_capture(_result(60, 61, rms=0.0, nonzero=0.0), report)
    assert any(name == "remote track" and status == "FAIL" for status, name, _ in report.items)


def test_names_judge_accepts_whisper_spelling():
    document = {
        "speaker_map": {
            "Speaker 2": {"name": "Alex", "status": "confirmed"},
            "Speaker 3": {"name": "Mahmood", "status": "confirmed"},
            "Speaker 4": {"name": "Ravi", "status": "confirmed"},
        },
        "segments": [{"speaker": "Alex", "track": "others"}],
    }
    assert selftest.judge_names(document, selftest.Report(io.StringIO()))
    document["speaker_map"]["Speaker 4"] = {"name": None, "status": "unknown", "display": "Speaker 4"}
    assert not selftest.judge_names(document, selftest.Report(io.StringIO()))


def test_recording_now_reads_heartbeat(tmp_path):
    (tmp_path / "heartbeat.json").write_text('{"state": "recording"}')
    assert selftest.recording_now(str(tmp_path))
