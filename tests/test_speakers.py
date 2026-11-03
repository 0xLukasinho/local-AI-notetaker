"""Speaker alignment. Whisper and the microphone are fakes."""

import array
import json
import sys
import wave

from meeting_notes.speakers import (
    THEM,
    align_mixed,
    align_tracks,
    choose_self_label,
    coerce_event,
    load_speaker_events,
    produce_transcript,
    render_transcript,
    speaker_for_interval,
    track_has_speech,
)


def _wav(path, amplitude, n=1600):
    frames = array.array("h", [int(amplitude)] * n)
    if sys.byteorder != "little":
        frames.byteswap()
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(frames.tobytes())


def _event(t, active=None, participants=None, self_name="", self_active=False, active_ids=None):
    body = {
        "t": t,
        "active": active or [],
        "participants": participants or [],
        "self": self_name,
        "self_active": self_active,
    }
    if active_ids:
        body["active_ids"] = active_ids
    return coerce_event(body)


def _hold(start, end, step, **kwargs):
    """Samples every `step` seconds so coverage spans the whole range."""
    moment = start
    events = []
    while moment < end:
        events.append(_event(moment, **kwargs))
        moment += step
    return events


def test_silence_and_speech_tracks(tmp_path):
    silent = tmp_path / "silent.wav"
    loud = tmp_path / "loud.wav"
    _wav(silent, 0)
    _wav(loud, 8000)
    assert track_has_speech(silent) is False
    assert track_has_speech(loud) is True
    assert track_has_speech(tmp_path / "missing.wav") is True


def test_microphone_is_self_and_loopback_without_names_is_them():
    labeled = align_tracks(
        [{"start": 0.2, "end": 1.4, "text": "I can take the notes"}],
        [{"start": 2.0, "end": 3.2, "text": "Thanks, that works"}],
        events=[],
        self_label="Alex",
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Alex", "I can take the notes"),
        (THEM, "Thanks, that works"),
    ]
    assert render_transcript(labeled) == (
        "[00:00] Alex: I can take the notes\n[00:02] Them: Thanks, that works"
    )


def test_remote_names_follow_the_active_speaker():
    events = []
    events += _hold(0.0, 3.0, 1.0, active=["Ada Lovelace"], participants=["Ada Lovelace", "Grace Hopper"])
    events += _hold(3.0, 6.0, 1.0, active=["Grace Hopper"], participants=["Ada Lovelace", "Grace Hopper"])
    labeled = align_tracks(
        [{"start": 0.4, "end": 1.0, "text": "hello"}],
        [
            {"start": 1.0, "end": 2.5, "text": "morning"},
            {"start": 3.2, "end": 5.0, "text": "shipping Friday"},
        ],
        events,
        "You",
    )
    by_text = {item["text"]: item["speaker"] for item in labeled}
    assert by_text["hello"] == "You"
    assert by_text["morning"] == "Ada Lovelace"
    assert by_text["shipping Friday"] == "Grace Hopper"


def test_words_split_a_remote_segment_across_two_speakers():
    events = _hold(0.0, 2.0, 0.5, active=["Ada Lovelace"]) + _hold(
        2.0, 4.0, 0.5, active=["Grace Hopper"]
    )
    labeled = align_tracks(
        [],
        [
            {
                "start": 0.0,
                "end": 4.0,
                "text": "budget then timeline",
                "words": [
                    {"start": 0.1, "end": 0.8, "text": "budget"},
                    {"start": 2.2, "end": 3.4, "text": "timeline"},
                ],
            }
        ],
        events,
        "You",
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Ada Lovelace", "budget"),
        ("Grace Hopper", "timeline"),
    ]


def test_overlapping_remote_speakers_are_both_named_when_neither_dominates():
    events = []
    for moment in (0.0, 0.5, 1.0, 1.5):
        events.append(_event(moment, active=["Ada Lovelace", "Grace Hopper"]))
    label = speaker_for_interval(0.0, 2.0, events)
    assert label == "Ada Lovelace / Grace Hopper"


def test_a_clear_majority_keeps_one_name():
    events = _hold(0.0, 4.0, 0.5, active=["Ada Lovelace"])
    events.append(_event(3.6, active=["Grace Hopper"]))
    assert speaker_for_interval(0.0, 4.0, events) == "Ada Lovelace"


def test_local_meet_name_is_not_used_for_remote_audio():
    # The extension marks the local user with self, so they are not in `active`.
    events = _hold(0.0, 2.0, 0.5, self_name="Alex Chen", self_active=True)
    labeled = align_tracks(
        [],
        [{"start": 0.2, "end": 1.6, "text": "echo of me"}],
        events,
        "Alex",
    )
    assert labeled[0]["speaker"] == THEM


def test_anonymous_ids_become_stable_speaker_letters_then_real_names():
    unnamed = _hold(0.0, 2.0, 0.5, active_ids=["device-b"]) + _hold(
        2.0, 4.0, 0.5, active_ids=["device-a"]
    )
    labeled = align_tracks(
        [],
        [
            {"start": 0.1, "end": 1.5, "text": "first"},
            {"start": 2.1, "end": 3.5, "text": "second"},
        ],
        unnamed,
        "You",
    )
    assert [item["speaker"] for item in labeled] == ["Speaker A", "Speaker B"]

    named_later = unnamed + [
        coerce_event(
            {
                "t": 10.0,
                "active": [{"id": "device-b", "name": "Ada Lovelace"}],
                "participants": [{"id": "device-b", "name": "Ada Lovelace"}],
            }
        )
    ]
    resolved = align_tracks(
        [],
        [{"start": 0.1, "end": 1.5, "text": "first"}],
        named_later,
        "You",
    )
    assert resolved[0]["speaker"] == "Ada Lovelace"


def test_bleed_into_the_mic_keeps_the_named_remote_line():
    events = _hold(0.0, 3.0, 0.5, active=["Ada Lovelace"])
    labeled = align_tracks(
        [{"start": 0.1, "end": 2.4, "text": "We should ship the notes Friday"}],
        [{"start": 0.2, "end": 2.5, "text": "We should ship the notes Friday"}],
        events,
        "Alex",
    )
    assert [(item["speaker"], item["track"]) for item in labeled] == [("Ada Lovelace", "others")]


def test_echo_on_the_loopback_keeps_the_microphone_line():
    labeled = align_tracks(
        [{"start": 0.2, "end": 2.0, "text": "I will send the notes tonight"}],
        [{"start": 0.3, "end": 2.1, "text": "I will send the notes tonight"}],
        events=[],
        self_label="Alex",
    )
    assert [(item["speaker"], item["track"]) for item in labeled] == [("Alex", "self")]


def test_real_overlap_keeps_both_lines():
    events = _hold(0.0, 2.0, 0.5, active=["Ada Lovelace"])
    labeled = align_tracks(
        [{"start": 0.0, "end": 1.5, "text": "I agree with that"}],
        [{"start": 0.1, "end": 1.6, "text": "We should ship Friday"}],
        events,
        "Alex",
    )
    assert {(item["speaker"], item["text"]) for item in labeled} == {
        ("Alex", "I agree with that"),
        ("Ada Lovelace", "We should ship Friday"),
    }


def test_mixed_audio_uses_meet_self_when_the_config_name_is_you():
    assert choose_self_label("You", [_event(0, self_name="Alex Chen")]) == "Alex Chen"
    assert choose_self_label("Alex", [_event(0, self_name="Alex Chen")]) == "Alex"
    events = _hold(0.0, 2.0, 0.5, self_name="Alex Chen", self_active=True) + _hold(
        2.0, 4.0, 0.5, active=["Ada Lovelace"], self_name="Alex Chen"
    )
    labeled = align_mixed(
        [
            {"start": 0.2, "end": 1.4, "text": "from me"},
            {"start": 2.2, "end": 3.4, "text": "from Ada"},
        ],
        events,
        "You",
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Alex Chen", "from me"),
        ("Ada Lovelace", "from Ada"),
    ]


def test_produce_transcript_writes_names_participants_and_json_shape(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    _wav(folder / "audio.wav", 8000)
    _wav(folder / "audio-self.wav", 8000)
    _wav(folder / "audio-others.wav", 8000)
    events = _hold(
        1.0,
        3.0,
        0.5,
        active=["Ada Lovelace"],
        participants=["Ada Lovelace", "Grace Hopper"],
        self_name="Alex Chen",
    )
    with open(folder / "speakers.jsonl", "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
        handle.write("{not json\n")

    def plain(_path):
        raise AssertionError("mixed file should not be transcribed when tracks exist")

    def many(paths):
        found = []
        for path in paths:
            if path.endswith("audio-self.wav"):
                found.append([{"start": 0.2, "end": 0.9, "text": "my update"}])
            else:
                found.append([{"start": 1.1, "end": 2.4, "text": "sounds good"}])
        return found

    text, document = produce_transcript(
        str(folder),
        self_name="You",
        enabled=True,
        transcribe_plain=plain,
        transcribe_paths=many,
    )
    assert text == "[00:00] Alex Chen: my update\n[00:01] Ada Lovelace: sounds good"
    assert document["self_name"] == "Alex Chen"
    assert document["meet_self_name"] == "Alex Chen"
    assert document["speaker_source"] == "meet"
    assert document["participants"] == ["Alex Chen", "Ada Lovelace", "Grace Hopper"]
    assert document["segments"][1]["speaker"] == "Ada Lovelace"
    assert document["limits"]
    assert load_speaker_events(str(folder / "speakers.jsonl"))


def test_produce_without_tracks_or_events_stays_unlabeled(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    (folder / "audio.wav").write_bytes(b"RIFFaudio")

    def plain(_path):
        return "[00:01] hello"

    def many(_paths):
        raise AssertionError("no detailed transcribe without tracks or events")

    text, document = produce_transcript(
        str(folder),
        self_name="Alex",
        enabled=True,
        transcribe_plain=plain,
        transcribe_paths=many,
    )
    assert text == "[00:01] hello"
    assert document is None


def test_labels_off_keeps_the_plain_transcript(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    _wav(folder / "audio-self.wav", 8000)

    def plain(_path):
        return "[00:03] untouched"

    text, document = produce_transcript(
        str(folder),
        self_name="Alex",
        enabled=False,
        transcribe_plain=plain,
        transcribe_paths=lambda _paths: [],
    )
    assert text == "[00:03] untouched"
    assert document is None


def test_gap_in_speaker_samples_falls_back_to_them():
    events = [_event(0.0, active=["Ada Lovelace"])]
    assert speaker_for_interval(10.0, 12.0, events) == THEM
    assert speaker_for_interval(0.2, 1.0, events) == "Ada Lovelace"
