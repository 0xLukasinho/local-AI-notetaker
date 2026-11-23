import array
import json
import os
import wave

from meeting_notes import speakers
from meeting_notes.transcriber import DEFAULT_VOCABULARY, _select_device, vocabulary_prompt


def _wav(path, samples):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(array.array("h", samples).tobytes())


def test_digital_silence_detection(tmp_path):
    zeros = tmp_path / "z.wav"
    quiet = tmp_path / "q.wav"
    _wav(zeros, [0] * 32000)
    _wav(quiet, [0] * 31999 + [1])
    assert speakers.track_is_digital_silence(zeros)
    assert not speakers.track_is_digital_silence(quiet)
    assert not speakers.track_is_digital_silence(tmp_path / "missing.wav")


def _session(tmp_path, others):
    loud = [int(8000 * ((i % 40) / 20.0 - 1)) for i in range(32000)]
    _wav(tmp_path / "audio.wav", loud)
    _wav(tmp_path / speakers.SELF_TRACK, loud)
    _wav(tmp_path / speakers.OTHERS_TRACK, others)
    return str(tmp_path)


def _fake_paths(paths):
    return [[{"start": 0.0, "end": 1.5, "text": "hello there from the call"}] for _ in paths]


def test_silent_others_track_is_unattributed(tmp_path):
    folder = _session(tmp_path, [0] * 32000)
    text, document = speakers.produce_transcript(
        folder, "Lukas", True, transcribe_plain=lambda p: "", transcribe_paths=_fake_paths
    )
    assert "Unattributed:" in text
    assert "Lukas:" not in text
    assert document["speaker_source"] == "mic-only"
    assert "warning" in document


def test_missing_others_track_keeps_self_label(tmp_path):
    folder = _session(tmp_path, [0] * 32000)
    os.remove(os.path.join(folder, speakers.OTHERS_TRACK))
    text, document = speakers.produce_transcript(
        folder, "Lukas", True, transcribe_plain=lambda p: "", transcribe_paths=_fake_paths
    )
    assert "Lukas:" in text
    assert "warning" not in document


def test_vocabulary_prompt_and_device():
    assert vocabulary_prompt("") is None
    assert "Uniswap" in vocabulary_prompt(DEFAULT_VOCABULARY)
    assert _select_device("cpu") == ("cpu", "int8")
    assert _select_device("mps")[0] == "mps"
