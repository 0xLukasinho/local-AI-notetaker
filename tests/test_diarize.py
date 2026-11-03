"""Voice clustering. Neural backends are not installed here; the builtin path is."""

import math
import wave

import pytest

from meeting_notes.diarize import _safe_reason, cluster_windows, diarize_wav


def _unit(axis, noise=0.0):
    vector = [0.0, 0.0, 0.0]
    vector[axis] = 1.0
    vector[(axis + 1) % 3] = noise
    return vector


def test_distinct_embeddings_stay_in_different_clusters():
    windows = []
    moment = 0.0
    for axis in (0, 1, 2):
        for step in range(4):
            windows.append(
                {
                    "start": moment,
                    "end": moment + 0.5,
                    "vector": _unit(axis, noise=0.01 * step),
                }
            )
            moment += 0.5
        moment += 0.4
    segments = cluster_windows(windows)
    ids = []
    for axis in (0, 1, 2):
        region = [item["cluster"] for item in segments if _covers(item, axis)]
        assert region
        assert len(set(region)) == 1
        ids.append(region[0])
    assert len(set(ids)) == 3
    assert all(" / " not in cluster for cluster in ids)


def _covers(segment, axis):
    # Each voice occupies 4 * 0.5s plus a following 0.4s gap, starting at axis * 2.4.
    start = axis * 2.4 + 0.2
    end = start + 1.2
    return segment["end"] > start and segment["start"] < end


def test_one_voice_stays_one_cluster():
    windows = []
    for index in range(8):
        windows.append(
            {
                "start": index * 0.6,
                "end": index * 0.6 + 0.55,
                "vector": _unit(0, noise=0.005 * index),
            }
        )
    segments = cluster_windows(windows)
    assert len({item["cluster"] for item in segments}) == 1


def test_far_apart_single_windows_are_not_merged():
    windows = [
        {"start": 0.0, "end": 1.0, "vector": [1.0, 0.0, 0.0]},
        {"start": 1.2, "end": 2.2, "vector": [0.0, 1.0, 0.0]},
        {"start": 2.4, "end": 3.4, "vector": [0.0, 0.0, 1.0]},
    ]
    segments = cluster_windows(windows)
    assert len({item["cluster"] for item in segments}) == 3


def test_leader_compression_on_a_long_mix_keeps_two_voices():
    windows = []
    moment = 0.0
    for block in range(30):
        voice = [1.0, 0.0, 0.0] if block % 2 == 0 else [0.0, 1.0, 0.0]
        for _step in range(12):
            windows.append({"start": moment, "end": moment + 0.4, "vector": list(voice)})
            moment += 0.4
        moment += 0.3
    assert len(windows) > 280
    segments = cluster_windows(windows)
    even = _majority(segments, 0.2, 4.0)
    odd_start = 12 * 0.4 + 0.3
    odd = _majority(segments, odd_start + 0.2, odd_start + 3.0)
    assert even != odd


def test_errors_do_not_repeat_a_huggingface_token():
    reason = _safe_reason("pyannote", RuntimeError("401 for hf_testtoken_value"), "hf_testtoken_value")
    assert "hf_testtoken_value" not in reason
    assert reason.startswith("pyannote failed")


def test_backend_off_does_not_read_audio(tmp_path):
    result = diarize_wav(str(tmp_path / "missing.wav"), backend="off", hf_token="hf_testtoken_value")
    assert result["segments"] == []
    assert result["backend"] is None
    assert "hf_testtoken_value" not in result["reason"]


def _majority(segments, start, end):
    scores = {}
    for segment in segments:
        lo = max(start, segment["start"])
        hi = min(end, segment["end"])
        if hi > lo:
            cluster = segment["cluster"]
            scores[cluster] = scores.get(cluster, 0.0) + (hi - lo)
    assert scores, (start, end, segments)
    return max(scores, key=lambda cluster: (scores[cluster], cluster))


def _formant(f0, formants, dur, seed, noise=0.02):
    numpy = pytest.importorskip("numpy")
    rng = numpy.random.default_rng(seed)
    rate = 16000
    count = int(rate * dur)
    t = numpy.arange(count) / rate
    f0c = f0 * (1 + 0.03 * numpy.sin(2 * math.pi * 3 * t) + 0.015 * rng.normal(size=count))
    phase = 2 * math.pi * numpy.cumsum(f0c / rate)
    buzz = numpy.zeros(count)
    for harmonic, amp in enumerate((1, 0.75, 0.55, 0.4, 0.3, 0.22, 0.16, 0.1, 0.07), start=1):
        buzz += amp * numpy.sin(harmonic * phase)
    buzz += noise * rng.normal(size=count)
    spectrum = numpy.fft.rfft(buzz)
    freqs = numpy.fft.rfftfreq(count, 1 / rate)
    envelope = numpy.zeros_like(freqs)
    for center, bandwidth, gain in formants:
        envelope += gain * numpy.exp(-0.5 * ((freqs - center) / bandwidth) ** 2)
    envelope = numpy.maximum(envelope, 0.015)
    rendered = numpy.fft.irfft(spectrum * envelope, n=count)
    rendered = rendered / (numpy.max(numpy.abs(rendered)) + 1e-9) * 0.55
    fade = int(0.02 * rate)
    rendered[:fade] *= numpy.linspace(0, 1, fade)
    rendered[-fade:] *= numpy.linspace(1, 0, fade)
    return rendered


def _silence(dur):
    numpy = pytest.importorskip("numpy")
    return numpy.zeros(int(16000 * dur))


def _write(path, audio):
    numpy = pytest.importorskip("numpy")
    clipped = numpy.clip(audio, -1, 1)
    frames = (clipped * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(frames)


def test_builtin_diarizer_separates_three_synthetic_voices(tmp_path):
    pytest.importorskip("numpy")
    import numpy as np

    vowel_a = [(700, 90, 1.0), (1200, 110, 0.55), (2500, 140, 0.28)]
    vowel_b = [(300, 70, 1.0), (2100, 120, 0.75), (3000, 140, 0.35)]
    vowel_c = [(500, 80, 1.0), (1700, 100, 0.5), (2500, 130, 0.3)]
    parts = [
        ("A", _formant(115, vowel_a, 2.0, 1)),
        ("", _silence(0.45)),
        ("B", _formant(180, vowel_b, 2.0, 2)),
        ("", _silence(0.45)),
        ("C", _formant(140, vowel_c, 2.0, 5)),
        ("", _silence(0.4)),
        ("A", _formant(118, vowel_a, 1.6, 3)),
    ]
    audio = np.concatenate([chunk for _name, chunk in parts])
    path = tmp_path / "others.wav"
    _write(path, audio)
    result = diarize_wav(str(path), backend="builtin")
    assert result["backend"] == "builtin"
    segments = result["segments"]
    assert segments
    cursor = 0.0
    regions = {}
    for name, chunk in parts:
        dur = len(chunk) / 16000.0
        if name:
            regions.setdefault(name, []).append(_majority(segments, cursor + 0.25, cursor + dur - 0.25))
        cursor += dur
    assert len(set(regions["A"])) == 1
    assert regions["A"][0] != regions["B"][0]
    assert regions["B"][0] != regions["C"][0]
    assert regions["A"][0] != regions["C"][0]


def test_builtin_diarizer_keeps_one_synthetic_voice(tmp_path):
    pytest.importorskip("numpy")
    import numpy as np

    vowel = [(700, 90, 1.0), (1200, 110, 0.55), (2500, 140, 0.28)]
    shifted = [(760, 90, 1.0), (1250, 110, 0.5), (2600, 140, 0.3)]
    parts = [
        _formant(115, vowel, 2.2, 1),
        _silence(0.5),
        _formant(128, vowel, 2.0, 6),
        _silence(0.4),
        _formant(110, shifted, 1.6, 7),
    ]
    path = tmp_path / "one.wav"
    _write(path, np.concatenate(parts))
    result = diarize_wav(str(path), backend="builtin")
    assert result["backend"] == "builtin"
    assert len({item["cluster"] for item in result["segments"]}) == 1
