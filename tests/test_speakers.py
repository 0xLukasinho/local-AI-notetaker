"""Speaker alignment. Whisper and the microphone are fakes."""

import array
import json
import os
import sys
import wave

import pytest

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


def _event(
    t,
    active=None,
    participants=None,
    self_name="",
    self_active=False,
    active_ids=None,
    captions=None,
    caption_speaker="",
):
    body = {
        "t": t,
        "active": active or [],
        "participants": participants or [],
        "self": self_name,
        "self_active": self_active,
    }
    if active_ids:
        body["active_ids"] = active_ids
    if captions:
        body["captions"] = captions
    if caption_speaker:
        body["caption_speaker"] = caption_speaker
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


def test_overlapping_tiles_are_not_merged_into_one_label():
    events = []
    for moment in (0.0, 0.5, 1.0, 1.5):
        events.append(
            _event(
                moment,
                active=["Ada Lovelace", "Grace Hopper"],
                participants=["You", "Ada Lovelace", "Grace Hopper"],
                self_name="You",
            )
        )
    labeled = align_tracks(
        [],
        [{"start": 0.1, "end": 1.6, "text": "someone spoke"}],
        events,
        "You",
    )
    assert labeled[0]["speaker"] == "Remote"
    assert " / " not in labeled[0]["speaker"]


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
    assert document["captions"] == "off"
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


def test_meet_dom_heuristic_and_captions():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = os.path.join(os.path.dirname(__file__), "meet_dom_check.js")
    result = subprocess.run([node, script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_gap_in_speaker_samples_falls_back_to_them():
    events = [_event(0.0, active=["Ada Lovelace"])]
    assert speaker_for_interval(10.0, 12.0, events) == THEM
    assert speaker_for_interval(0.2, 1.0, events) == "Ada Lovelace"


def _both_active(moment):
    return _event(
        moment,
        active=["Daniel Bronheim", "Mahmoud Lababidi"],
        participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
        self_name="Lukas",
    )


def test_three_voices_are_not_merged_when_tiles_flag_everyone():
    events = [_both_active(moment) for moment in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5)]
    voices = [
        {"start": 0.0, "end": 1.8, "cluster": "0"},
        {"start": 2.0, "end": 3.8, "cluster": "1"},
    ]
    labeled = align_tracks(
        [],
        [
            {
                "start": 0.1,
                "end": 1.6,
                "text": "budget approved",
                "words": [
                    {"start": 0.1, "end": 0.6, "text": "budget"},
                    {"start": 0.8, "end": 1.5, "text": "approved"},
                ],
            },
            {
                "start": 2.1,
                "end": 3.4,
                "text": "timeline friday",
                "words": [
                    {"start": 2.1, "end": 2.6, "text": "timeline"},
                    {"start": 2.8, "end": 3.3, "text": "friday"},
                ],
            },
        ],
        events,
        "Lukas",
        voice_segments=voices,
    )
    speakers = [item["speaker"] for item in labeled]
    assert speakers == ["Speaker 2", "Speaker 3"]
    assert all(" / " not in speaker for speaker in speakers)
    assert [item["text"] for item in labeled] == ["budget approved", "timeline friday"]


def test_captions_name_voice_clusters():
    events = [
        _event(
            0.2,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Daniel Bronheim", "text": "budget"}],
        ),
        _event(
            1.0,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Daniel Bronheim", "text": "budget approved"}],
        ),
        _event(
            2.2,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[
                {"speaker": "Daniel Bronheim", "text": "budget approved"},
                {"speaker": "Mahmoud Lababidi", "text": "timeline"},
            ],
        ),
        _event(
            3.1,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[
                {"speaker": "Daniel Bronheim", "text": "budget approved"},
                {"speaker": "Mahmoud Lababidi", "text": "timeline friday"},
            ],
        ),
    ]
    voices = [
        {"start": 0.0, "end": 1.9, "cluster": "0"},
        {"start": 2.1, "end": 3.8, "cluster": "1"},
    ]
    labeled = align_tracks(
        [],
        [
            {"start": 0.2, "end": 1.6, "text": "budget approved"},
            {"start": 2.3, "end": 3.4, "text": "timeline friday"},
        ],
        events,
        "Lukas",
        voice_segments=voices,
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Daniel Bronheim", "budget approved"),
        ("Mahmoud Lababidi", "timeline friday"),
    ]


def test_exclusive_tiles_name_voice_clusters():
    events = []
    events += _hold(
        0.0,
        2.0,
        0.4,
        active=["Daniel Bronheim"],
        participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
        self_name="Lukas",
    )
    events += _hold(
        2.0,
        4.0,
        0.4,
        active=["Mahmoud Lababidi"],
        participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
        self_name="Lukas",
    )
    voices = [
        {"start": 0.0, "end": 1.8, "cluster": "0"},
        {"start": 2.1, "end": 3.8, "cluster": "1"},
    ]
    labeled = align_tracks(
        [],
        [
            {"start": 0.2, "end": 1.5, "text": "from daniel"},
            {"start": 2.2, "end": 3.5, "text": "from mahmoud"},
        ],
        events,
        "Lukas",
        voice_segments=voices,
    )
    assert [item["speaker"] for item in labeled] == ["Daniel Bronheim", "Mahmoud Lababidi"]


def test_caption_speaker_field_names_a_three_person_call():
    events = []
    for moment, speaker in (
        (0.0, "Daniel Bronheim"),
        (0.8, "Daniel Bronheim"),
        (1.6, "Mahmoud Lababidi"),
        (2.4, "Mahmoud Lababidi"),
    ):
        events.append(
            _event(
                moment,
                active=["Daniel Bronheim", "Mahmoud Lababidi"],
                participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
                self_name="Lukas",
                caption_speaker=speaker,
            )
        )
    labeled = align_tracks(
        [],
        [
            {
                "start": 0.1,
                "end": 2.8,
                "text": "alpha beta gamma",
                "words": [
                    {"start": 0.2, "end": 0.6, "text": "alpha"},
                    {"start": 0.9, "end": 1.3, "text": "beta"},
                    {"start": 1.8, "end": 2.6, "text": "gamma"},
                ],
            }
        ],
        events,
        "Lukas",
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Daniel Bronheim", "alpha beta"),
        ("Mahmoud Lababidi", "gamma"),
    ]
    assert all(" / " not in item["speaker"] for item in labeled)
    assert events[0]["caption_speaker"] == "Daniel Bronheim"


def test_captions_off_uses_one_tile_and_otherwise_remote():
    exclusive = _hold(
        0.0,
        2.0,
        0.4,
        active=["Daniel Bronheim"],
        participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
        self_name="Lukas",
    ) + _hold(
        2.0,
        4.0,
        0.4,
        active=["Mahmoud Lababidi"],
        participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
        self_name="Lukas",
    )
    named = align_tracks(
        [],
        [
            {"start": 0.2, "end": 1.4, "text": "from daniel"},
            {"start": 2.2, "end": 3.4, "text": "from mahmoud"},
        ],
        exclusive,
        "Lukas",
    )
    assert [item["speaker"] for item in named] == ["Daniel Bronheim", "Mahmoud Lababidi"]

    ambiguous = [_both_active(moment) for moment in (0.0, 0.5, 1.0, 1.5)]
    unlabeled = align_tracks(
        [],
        [{"start": 0.2, "end": 1.4, "text": "could be either"}],
        ambiguous,
        "Lukas",
    )
    assert unlabeled[0]["speaker"] == "Remote"


def test_captions_label_words_when_diarization_is_unavailable():
    events = [
        _event(
            0.0,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Daniel Bronheim", "text": "ship"}],
        ),
        _event(
            1.2,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Daniel Bronheim", "text": "ship friday"}],
        ),
        _event(
            2.6,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Mahmoud Lababidi", "text": "agreed"}],
        ),
        _event(
            3.6,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Mahmoud Lababidi", "text": "agreed today"}],
        ),
    ]
    labeled = align_tracks(
        [],
        [
            {
                "start": 0.1,
                "end": 3.8,
                "text": "ship friday agreed today",
                "words": [
                    {"start": 0.2, "end": 0.7, "text": "ship"},
                    {"start": 0.9, "end": 1.6, "text": "friday"},
                    {"start": 2.7, "end": 3.2, "text": "agreed"},
                    {"start": 3.3, "end": 3.8, "text": "today"},
                ],
            }
        ],
        events,
        "Lukas",
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Daniel Bronheim", "ship friday"),
        ("Mahmoud Lababidi", "agreed today"),
    ]
    assert all(" / " not in item["speaker"] for item in labeled)


def test_crosstalk_keeps_whole_turns_and_short_interjections():
    events = _hold(
        0.0,
        4.0,
        0.5,
        active=["Mahmoud Lababidi"],
        participants=["Lukas", "Mahmoud Lababidi", "Daniel Bronheim"],
        self_name="Lukas",
    )
    labeled = align_tracks(
        [
            {
                "start": 0.0,
                "end": 2.2,
                "text": "been working on it",
                "words": [
                    {"start": 0.05, "end": 0.3, "text": "been"},
                    {"start": 0.7, "end": 1.05, "text": "working"},
                    {"start": 1.7, "end": 2.1, "text": "on"},
                ],
            }
        ],
        [
            {
                "start": 0.2,
                "end": 2.4,
                "text": "said it already",
                "words": [
                    {"start": 0.35, "end": 0.6, "text": "said"},
                    {"start": 1.1, "end": 1.4, "text": "it"},
                    {"start": 2.15, "end": 2.4, "text": "already"},
                ],
            }
        ],
        events,
        "Lukas",
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Lukas", "been working on"),
        ("Mahmoud Lababidi", "said it already"),
    ]

    interjection = align_tracks(
        [{"start": 1.15, "end": 1.45, "text": "yeah"}],
        [
            {
                "start": 0.0,
                "end": 3.2,
                "text": "we should ship the notes friday",
                "words": [
                    {"start": 0.0, "end": 0.3, "text": "we"},
                    {"start": 0.4, "end": 0.8, "text": "should"},
                    {"start": 0.9, "end": 1.1, "text": "ship"},
                    {"start": 1.5, "end": 1.8, "text": "the"},
                    {"start": 1.9, "end": 2.2, "text": "notes"},
                    {"start": 2.4, "end": 2.9, "text": "friday"},
                ],
            }
        ],
        events,
        "Lukas",
    )
    assert [(item["speaker"], item["text"]) for item in interjection] == [
        ("Mahmoud Lababidi", "we should ship the notes friday"),
        ("Lukas", "yeah"),
    ]


def test_two_person_call_keeps_one_remote_name_without_the_speaking_ring():
    events = _hold(
        0.0,
        3.0,
        0.5,
        active=[],
        participants=["Lukas", "Daniel Bronheim"],
        self_name="Lukas",
    )
    labeled = align_tracks(
        [{"start": 0.1, "end": 0.8, "text": "hello"}],
        [{"start": 1.0, "end": 2.2, "text": "hi Lukas"}],
        events,
        "Lukas",
    )
    assert [(item["speaker"], item["text"]) for item in labeled] == [
        ("Lukas", "hello"),
        ("Daniel Bronheim", "hi Lukas"),
    ]


def test_captions_round_trip_and_drop_garbage():
    event = coerce_event(
        {
            "t": 1.5,
            "captions": [
                "nope",
                {"speaker": "Ada Lovelace", "text": "ship it"},
                {"text": "missing speaker"},
                {"speaker": "Grace Hopper", "text": "   "},
            ],
        }
    )
    assert event["captions"] == [{"speaker": "Ada Lovelace", "text": "ship it"}]
    assert event["caption_speaker"] == "Ada Lovelace"


def test_two_person_produce_does_not_diarize_or_split(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    _wav(folder / "audio-self.wav", 8000)
    _wav(folder / "audio-others.wav", 8000)
    events = _hold(
        0.0,
        2.0,
        0.5,
        active=["Daniel Bronheim"],
        participants=["Lukas", "Daniel Bronheim"],
        self_name="Lukas",
    )
    with open(folder / "speakers.jsonl", "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    def diarize(_path):
        raise AssertionError("a two-person call must not be diarized")

    text, document = produce_transcript(
        str(folder),
        self_name="Lukas",
        enabled=True,
        transcribe_plain=lambda _path: "",
        transcribe_paths=lambda paths: [
            [{"start": 0.2, "end": 0.8, "text": "my side"}] if path.endswith("audio-self.wav") else [
                {"start": 1.0, "end": 1.8, "text": "remote side"}
            ]
            for path in paths
        ],
        diarize=True,
        diarizer=diarize,
    )
    assert text == "[00:00] Lukas: my side\n[00:01] Daniel Bronheim: remote side"
    assert "diarization" not in document
    assert document["speaker_source"] == "meet"


def test_produce_names_three_people_from_injected_voices(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    _wav(folder / "audio-self.wav", 8000)
    _wav(folder / "audio-others.wav", 8000)
    events = [
        _event(
            0.2,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Daniel Bronheim", "text": "alpha"}],
        ),
        _event(
            1.1,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Daniel Bronheim", "text": "alpha beta"}],
        ),
        _event(
            2.2,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Mahmoud Lababidi", "text": "gamma"}],
        ),
        _event(
            3.0,
            active=["Daniel Bronheim", "Mahmoud Lababidi"],
            participants=["Lukas", "Daniel Bronheim", "Mahmoud Lababidi"],
            self_name="Lukas",
            captions=[{"speaker": "Mahmoud Lababidi", "text": "gamma delta"}],
        ),
    ]
    with open(folder / "speakers.jsonl", "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    def diarize(path):
        assert path.endswith("audio-others.wav")
        return {
            "backend": "test",
            "segments": [
                {"start": 0.0, "end": 1.8, "cluster": "voice-a"},
                {"start": 2.0, "end": 3.6, "cluster": "voice-b"},
            ],
        }

    text, document = produce_transcript(
        str(folder),
        self_name="Lukas",
        enabled=True,
        transcribe_plain=lambda _path: "",
        transcribe_paths=lambda paths: [
            [{"start": 0.1, "end": 0.4, "text": "checking"}]
            if path.endswith("audio-self.wav")
            else [
                {
                    "start": 0.2,
                    "end": 3.4,
                    "text": "alpha beta gamma delta",
                    "words": [
                        {"start": 0.3, "end": 0.8, "text": "alpha"},
                        {"start": 1.0, "end": 1.5, "text": "beta"},
                        {"start": 2.2, "end": 2.7, "text": "gamma"},
                        {"start": 2.9, "end": 3.4, "text": "delta"},
                    ],
                }
            ]
            for path in paths
        ],
        diarize=True,
        diarizer=diarize,
    )
    assert "Daniel Bronheim: alpha beta" in text
    assert "Mahmoud Lababidi: gamma delta" in text
    assert " / " not in text
    assert document["diarization"] == {"backend": "test", "clusters": 2}
    assert document["segments"][0]["speaker"] == "Lukas"
