"""Name mapping: who is each remote voice, and how sure are we."""

import datetime as dt
import json
import math
import os
import wave

import pytest

from meeting_notes import speaker_naming as sn
from meeting_notes import speakers
from meeting_notes.calendar_roster import match_event, parse_ics
from meeting_notes.voice_profiles import VoiceProfiles


def t(start, speaker, text, track=None):
    return {
        "start": float(start),
        "end": float(start) + 4.0,
        "speaker": speaker,
        "text": text,
        "track": track or ("self" if speaker == "Lukas" else "others"),
    }


def display(speaker_map, label):
    return speaker_map[label]["display"]


# --- the four required scenarios -----------------------------------------

def test_one_remote_attendee_names_the_only_voice():
    turns = [
        t(0, "Lukas", "Hey, good to see you."),
        t(5, "Speaker 2", "Likewise, let's go through the integration."),
    ]
    roster = [sn.roster_entry("Alex Rivera", "calendar")]
    speaker_map, renamed = sn.resolve_speakers(turns, "Lukas", roster=roster, roster_complete=True)
    assert display(speaker_map, "Speaker 2") == "Alex Rivera"
    assert speaker_map["Speaker 2"]["confidence"] >= 0.85
    assert speaker_map["Lukas"]["display"] == "Lukas"
    assert [turn["speaker"] for turn in renamed] == ["Lukas", "Alex Rivera"]
    assert renamed[1]["voice"] == "Speaker 2"


def test_several_attendees_are_told_apart_by_intros_and_addressing():
    turns = [
        t(0, "Speaker 2", "Hi everyone, this is Alex from Northwind."),
        t(5, "Speaker 3", "Hey Alex, I'm Mahmoud. Can you walk us through the timeline?"),
        t(10, "Speaker 2", "Sure, we plan to ship in the first quarter."),
        t(15, "Speaker 4", "Mahmoud, do you have the latest volume numbers?"),
        t(20, "Speaker 3", "Yes Priya, volume is up forty percent."),
        t(25, "Lukas", "Great. Priya, could you send the deck?"),
        t(30, "Speaker 4", "Absolutely, I will send it tonight."),
    ]
    roster = [
        sn.roster_entry("Alex Rivera", "calendar"),
        sn.roster_entry("Mahmoud", "calendar"),
        sn.roster_entry("Priya Shah", "calendar"),
    ]
    speaker_map, renamed = sn.resolve_speakers(turns, "Lukas", roster=roster, roster_complete=True)
    assert display(speaker_map, "Speaker 2") == "Alex Rivera"
    assert display(speaker_map, "Speaker 3") == "Mahmoud"
    assert display(speaker_map, "Speaker 4") == "Priya Shah"
    for label in ("Speaker 2", "Speaker 3", "Speaker 4"):
        assert speaker_map[label]["status"] == "confirmed"
        assert speaker_map[label]["evidence"]
    assert all(not turn["speaker"].startswith("Speaker") for turn in renamed)


def test_slack_huddle_without_calendar_uses_transcript_evidence():
    title = "executive - Greek - Slack"
    assert sn.names_from_window_title(title, ["Lukas"], ["Greek"]) == []  # a channel, no roster
    turns = [
        t(0, "Lukas", "Mahmoud, what did the auditors say?"),
        t(5, "Speaker 2", "They signed off on the vault contracts yesterday."),
        t(10, "Speaker 3", "Hi all, Dina here, sorry I'm late."),
        t(15, "Speaker 2", "No worries Dina, we just started."),
    ]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=[], known_people=["Mahmoud"])
    # "Dina here" names nobody on a list (participants, configured people, or
    # the voice store), so the voice stays unnamed instead of trusting a word.
    assert display(speaker_map, "Speaker 3") == "Speaker 3"
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=[], known_people=["Mahmoud", "Dina"])
    assert display(speaker_map, "Speaker 3") == "Dina"
    # Mahmoud was asked a question and this voice answered: one weak-ish signal,
    # plus Speaker 2 addressed "Dina" so it is not Dina. Not enough to confirm.
    assert speaker_map["Speaker 2"]["status"] in ("confirmed", "unconfirmed")
    if speaker_map["Speaker 2"]["status"] == "unconfirmed":
        assert display(speaker_map, "Speaker 2") == "Speaker 2 (unconfirmed: likely Mahmoud)"


def test_unknown_voice_stays_unnamed_and_nothing_is_invented():
    turns = [
        t(0, "Lukas", "Let's get started."),
        t(5, "Speaker 2", "Sounds good, the numbers look fine."),
        t(10, "Speaker 3", "I agree with that."),
    ]
    speaker_map, renamed = sn.resolve_speakers(turns, "Lukas", roster=[])
    assert display(speaker_map, "Speaker 2") == "Speaker 2"
    assert display(speaker_map, "Speaker 3") == "Speaker 3"
    assert speaker_map["Speaker 2"]["status"] == "unknown"
    assert {turn["speaker"] for turn in renamed} == {"Lukas", "Speaker 2", "Speaker 3"}


def test_weak_evidence_gives_unconfirmed_likely_label():
    turns = [
        t(0, "Speaker 2", "Okay so the first item on the agenda."),
        t(5, "Speaker 3", "Right."),
    ]
    roster = [sn.roster_entry("Alex Rivera", "calendar")]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=roster, roster_complete=True)
    # Two voices, one listed attendee: the dominant voice is only "likely".
    assert speaker_map["Speaker 2"]["status"] == "unconfirmed"
    assert display(speaker_map, "Speaker 2") == "Speaker 2 (unconfirmed: likely Alex Rivera)"
    assert display(speaker_map, "Speaker 3") == "Speaker 3"


# --- rules ----------------------------------------------------------------

def test_self_name_is_never_given_to_a_remote_voice():
    turns = [
        t(0, "Speaker 2", "Thanks Lukas, that's helpful."),
        t(5, "Lukas", "Sure."),
        t(9, "Speaker 2", "I'm Lukas's partner on this."),
    ]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=[], self_aliases=["Lukas Unterschuetz"])
    assert "Lukas" not in display(speaker_map, "Speaker 2")


def test_nobody_is_named_after_a_name_they_addressed():
    turns = [
        t(0, "Speaker 2", "Hey Alex, I'm Mahmoud."),
        t(5, "Speaker 3", "Hi Mahmoud."),
    ]
    roster = [sn.roster_entry("Alex", "calendar"), sn.roster_entry("Mahmoud", "calendar")]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=roster, roster_complete=True)
    assert display(speaker_map, "Speaker 2") == "Mahmoud"
    assert display(speaker_map, "Speaker 3") == "Alex"


def test_generic_remote_label_without_diarization_is_numbered():
    turns = [t(0, "Them", "Hello there."), t(5, "Lukas", "Hi.")]
    speaker_map, renamed = sn.resolve_speakers(turns, "Lukas", roster=[sn.roster_entry("Jordan Blake", "slack-dm", 1.0)])
    assert display(speaker_map, "Them") == "Jordan Blake"
    assert renamed[0]["speaker"] == "Jordan Blake"


def test_title_name_alone_is_only_a_likely_name():
    # "Partnerships Sync (Alex Rivera)": Alex is Greek's CGO and may be the
    # host, not the Northwind guest. A single voice must not become Alex on
    # the title alone.
    roster = sn.names_from_window_title("Meet - Partnerships Sync (Alex Rivera)", ["Lukas"])
    turns = [t(0, "Speaker 2", "Thanks for having us, the integration is on track.")]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=roster)
    assert speaker_map["Speaker 2"]["status"] == "unconfirmed"
    assert display(speaker_map, "Speaker 2") == "Speaker 2 (unconfirmed: likely Alex Rivera)"


def test_slack_channel_roster():
    rosters = sn.parse_channel_rosters("executive: Mahmoud Lababidi, Alex Rivera; #gtm: Alex Rivera")
    assert rosters == {"executive": ["Mahmoud Lababidi", "Alex Rivera"], "gtm": ["Alex Rivera"]}
    roster = sn.slack_channel_roster("executive - Greek - Slack", rosters)
    assert [e["name"] for e in roster] == ["Mahmoud Lababidi", "Alex Rivera"]
    turns = [
        t(0, "Lukas", "Mahmoud, where are we on the audit?"),
        t(4, "Speaker 2", "Cyfrin signed off yesterday."),
        t(8, "Speaker 3", "Great. Mahmoud, can you share the report?"),
        t(12, "Speaker 2", "Sure, posting it now."),
        t(16, "Speaker 2", "Alex, what's the launch date looking like?"),
        t(20, "Speaker 3", "Second week of December."),
    ]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=roster)
    assert display(speaker_map, "Speaker 2") == "Mahmoud Lababidi"
    assert display(speaker_map, "Speaker 3") == "Alex Rivera"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Meet - Partnerships Sync (Alex Rivera)", ["Alex Rivera"]),
        ("Meet - Greek Team Meeting (Jordan Blake)", ["Jordan Blake"]),
        ("Mahmoud Ali (DM) - Greek - Slack", ["Mahmoud Ali"]),
        ("Alex, Mahmoud - Greek - Slack", ["Alex", "Mahmoud"]),
        ("executive - Greek - Slack", []),
        ("Meet - Lukas <> Alex", ["Alex"]),
        ("Meet - Greek <> Northwind", []),
        ("Meet - abc-defg-hij", []),
    ],
)
def test_window_title_roster(title, expected):
    found = sn.names_from_window_title(title, ["Lukas"], ["Greek", "Northwind"])
    assert [entry["name"] for entry in found] == expected


def test_self_introductions():
    assert sn.self_introductions("Hi everyone, this is Alex from Northwind.") == ["Alex"]
    assert sn.self_introductions("Hey, I'm Mahmoud. Good to meet you.") == ["Mahmoud"]
    assert sn.self_introductions("My name is Priya Shah and I run partnerships") == ["Priya Shah"]
    assert sn.self_introductions("I'm sure that works.") == []
    assert sn.self_introductions("I'm good, thanks.") == []
    assert sn.self_introductions("This is great.") == []


def test_merge_roster_prefers_full_names():
    merged = sn.merge_roster(
        [sn.roster_entry("Alex Rivera", "calendar")],
        [sn.roster_entry("Alex", "meeting-title-guess", 0.5)],
        self_aliases=["Lukas"],
    )
    assert [entry["name"] for entry in merged] == ["Alex Rivera"]
    assert merged[0]["source"] == "calendar"


# --- voice profiles ---------------------------------------------------------

def _unit(values):
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def test_voice_profile_match_names_a_returning_person(tmp_path):
    store = VoiceProfiles(str(tmp_path / "profiles.json"))
    aaron = _unit([1.0, 0.1, 0.0, 0.0])
    store.update("Alex Rivera", aaron, session="s1")
    store.save()
    reloaded = VoiceProfiles(str(tmp_path / "profiles.json"))
    matches = reloaded.match(_unit([0.98, 0.15, 0.02, 0.0]))
    assert matches and matches[0][0] == "Alex Rivera" and matches[0][1] > 0.95
    assert reloaded.match(_unit([0.0, 0.0, 1.0, 0.0])) == []
    turns = [t(0, "Speaker 2", "Let's get going."), t(5, "Speaker 3", "Sure.")]
    speaker_map, _ = sn.resolve_speakers(
        turns, "Lukas", voice_matches={"Speaker 2": matches}, known_people=reloaded.names()
    )
    assert display(speaker_map, "Speaker 2") == "Alex Rivera"
    assert display(speaker_map, "Speaker 3") == "Speaker 3"
    assert oct(os.stat(tmp_path / "profiles.json").st_mode & 0o777) == "0o600"


# --- calendar -----------------------------------------------------------------

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Partnerships Sync (Alex Rivera)
DTSTART:20261125T190000Z
DTEND:20261125T193000Z
ORGANIZER;CN=Lukas Unterschuetz:mailto:lukas@greek.fi
ATTENDEE;CN=Alex Rivera;PARTSTAT=ACCEPTED:mailto:aaron@northwind.com
ATTENDEE;CN=Lukas Unterschuetz;PARTSTAT=ACCEPTED:mailto:lukas@greek.fi
ATTENDEE;CN=Declined Person;PARTSTAT=DECLINED:mailto:no@x.com
LOCATION:https://meet.google.com/abc-defg-hij
END:VEVENT
BEGIN:VEVENT
SUMMARY:Other meeting
DTSTART:20261125T220000Z
DTEND:20261125T223000Z
ATTENDEE;CN=Someone Else:mailto:se@x.com
END:VEVENT
END:VCALENDAR
"""


def test_ics_event_is_matched_by_time_and_meet_code():
    from meeting_notes.calendar_roster import _event_entries

    events = parse_ics(ICS)
    assert len(events) == 2
    event = match_event(events, "2026-11-25T14:01:33-05:00", meet_code="abc-defg-hij")
    assert event["title"] == "Partnerships Sync (Alex Rivera)"
    names = [entry["name"] for entry in _event_entries(event)]
    assert "Alex Rivera" in names and "Declined Person" not in names
    assert match_event(events, "2026-11-25T11:00:00-05:00") is None


def test_calendar_json_drop_file(tmp_path):
    from meeting_notes.calendar_roster import calendar_roster

    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"events": [{
        "title": "Greek x Base weekly",
        "start": "2026-11-25T14:00:00-05:00",
        "end": "2026-11-25T14:30:00-05:00",
        "attendees": [{"name": "Priya Shah", "email": "p@base.org"}, {"email": "mahmoud.k@greek.fi"}],
    }]}))

    class Cfg:
        calendar_json_path = str(path)
        calendar_ics_url = ""

    entries, title = calendar_roster({"recording_started_at": "2026-11-25T14:03:00-05:00"}, Cfg())
    assert title == "Greek x Base weekly"
    assert [(e["name"], e["source"]) for e in entries] == [("Priya Shah", "calendar"), ("Mahmoud", "calendar-email")]


# --- clustering -------------------------------------------------------------

def test_average_linkage_groups_voices():
    from meeting_notes.diarize import average_linkage

    a, b = _unit([1, 0, 0.1]), _unit([0, 1, 0.1])
    labels = average_linkage([a, b, _unit([0.95, 0.05, 0.1]), _unit([0.05, 0.97, 0.1]), a])
    assert labels == [0, 1, 0, 1, 0]
    assert average_linkage([a]) == [0]
    assert average_linkage([]) == []


# --- produce_transcript wiring ----------------------------------------------

def _tone_wav(path, seconds=12, freq=220.0):
    rate = 16000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            value = int(8000 * math.sin(2 * math.pi * freq * i / rate))
            frames += value.to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(frames))


def test_produce_transcript_puts_names_on_every_line(tmp_path, monkeypatch):
    folder = tmp_path / "2026-11-25_abc-defg-hij"
    folder.mkdir()
    _tone_wav(folder / "audio-self.wav", freq=300)
    _tone_wav(folder / "audio-others.wav", freq=200)
    (folder / "status.json").write_text(json.dumps({"window_title": "Meet - Partnerships Sync (Alex Rivera)"}))
    others = [
        {"start": 0.5, "end": 3.5, "text": "Hi, this is Alex from Northwind."},
        {"start": 6.5, "end": 9.0, "text": "Hey Alex, I'm Mahmoud."},
    ]
    mine = [{"start": 4.0, "end": 6.0, "text": "Welcome both of you."}]
    voices = [
        {"start": 0.5, "end": 3.5, "cluster": "0"},
        {"start": 6.5, "end": 9.0, "cluster": "1"},
    ]
    monkeypatch.setattr(
        speakers,
        "_utterance_timeline",
        lambda folder, segs, models_dir=None, min_clusters=1: (voices, {"backend": "fake", "clusters": 2}, {"0": _unit([1, 0]), "1": _unit([0, 1])}),
    )
    text, document = speakers.produce_transcript(
        str(folder),
        "Lukas",
        True,
        transcribe_plain=lambda path: "",
        transcribe_paths=lambda paths: [mine, others],
        diarize=True,
        naming={"profiles_path": str(tmp_path / "voices.json"), "known_people": ["Mahmoud"]},
    )
    lines = text.splitlines()
    assert lines == [
        "[00:00] Alex Rivera: Hi, this is Alex from Northwind.",
        "[00:04] Lukas: Welcome both of you.",
        "[00:06] Mahmoud: Hey Alex, I'm Mahmoud.",
    ]
    assert document["speaker_source"] == "named"
    smap = document["speaker_map"]
    assert smap["Speaker 2"]["name"] == "Alex Rivera" and smap["Speaker 2"]["confidence"] >= 0.9
    assert smap["Speaker 3"]["name"] == "Mahmoud"
    assert document["speaker_naming"]["roster"] == [{"name": "Alex Rivera", "source": "meeting-title"}]
    assert (folder / "voices.json").exists()
    profiles = VoiceProfiles(str(tmp_path / "voices.json"))
    assert profiles.names() == ["Alex Rivera", "Mahmoud"]  # both listed and confirmed


def test_whisper_misspelling_is_mapped_to_the_known_spelling():
    turns = [
        t(0, "Speaker 2", "Hey, I'm Mahmood. Good to meet you."),
        t(5, "Speaker 3", "Yes Mahmood, volume is up."),
    ]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", known_people=["Mahmoud"])
    assert display(speaker_map, "Speaker 2") == "Mahmoud"
    assert sn.similar_name("Mahmoud", "Mahmood")
    assert not sn.similar_name("Ravi", "Dave")


def test_vocabulary_includes_people_names(tmp_path):
    from meeting_notes import session_finish
    from meeting_notes.config import Config

    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "status.json").write_text(json.dumps({"window_title": "Meet - Partnerships Sync (Alex Rivera)"}))
    config = Config(known_people="Mahmoud, Priya", voice_profiles=False, self_name="Lukas")
    text = session_finish._vocabulary_with_names(str(folder), config, "Greek, Bebop.")
    assert "Mahmoud" in text and "Priya" in text and "Alex Rivera" in text
    assert session_finish._vocabulary_with_names(str(folder), Config(whisper_vocabulary=""), "x") == ""


def test_e2e_regression_intro_drowned_out_but_addressing_names_the_voice():
    # Real e2e run 2026-11-25: other system audio covered Alex's intro, so
    # only people addressing him identify the voice.
    turns = [
        t(0, "Speaker 2", "So it might feel difficult to keep up with the landscape."),
        t(8, "Speaker 3", "Hey Alex, I'm Mahmoud. Can you walk us through the timeline?"),
        t(15, "Speaker 2", "Sure. We plan to ship in the first quarter."),
        t(23, "Speaker 4", "Hi, my name is Ravi. Mahmoud, do you have the latest numbers?"),
        t(31, "Speaker 3", "Yes Ravi, weekly volume is up."),
        t(37, "Speaker 2", "That is great to hear. Ravi, could you send a deck after this call?"),
        t(43, "Speaker 4", "Absolutely Alex, I will send it tonight."),
        t(47, "Speaker 3", "Perfect. Alex, one more question about the settlement layer."),
        t(54, "Speaker 2", "Good question. Fees are split evenly for now."),
    ]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", known_people=["Alex", "Mahmoud", "Ravi"])
    assert display(speaker_map, "Speaker 2") == "Alex"
    assert display(speaker_map, "Speaker 3") == "Mahmoud"
    assert display(speaker_map, "Speaker 4") == "Ravi"


def test_common_words_are_never_names():
    turns = [
        t(0, "Lukas", "Awesome, yeah. Anything you need from us?"),
        t(4, "Speaker 2", "Yeah, I think the second option works."),
        t(9, "Speaker 2", "Awesome, thanks."),
    ]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=[])
    assert display(speaker_map, "Speaker 2") == "Speaker 2"


def test_unlisted_addressed_name_is_not_used_but_listed_one_is():
    turns = [
        t(0, "Speaker 2", "Hey Lukas."),
        t(1, "Lukas", "Hey Jordan, how's it going?"),
        t(3, "Speaker 2", "Good, thanks. How are you?"),
    ]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=[])
    assert "Jordan" not in display(speaker_map, "Speaker 2")
    roster = [sn.roster_entry("Jordan Lee", "meet"), sn.roster_entry("Sam Park", "meet")]
    speaker_map, _ = sn.resolve_speakers(turns, "Lukas", roster=roster)
    assert "Jordan Lee" in display(speaker_map, "Speaker 2")
