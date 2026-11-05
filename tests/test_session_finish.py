"""Watcher stop must transcribe, summarize, and deliver, even if the child dies."""

import os

import meeting_notes.cli as cli
from meeting_notes.config import Config
from meeting_notes.session import read_status, write_status
from meeting_notes.session_finish import finish_folder, transcript_ready
from meeting_notes.watcher import MeetWatcher, RunningSession


class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += float(seconds)


class _Log:
    def __init__(self):
        self.messages = []

    def _fmt(self, msg, args):
        return msg % args if args else str(msg)

    def info(self, msg, *args):
        self.messages.append(self._fmt(msg, args))

    def warning(self, msg, *args):
        self.messages.append(self._fmt(msg, args))

    def error(self, msg, *args):
        self.messages.append(self._fmt(msg, args))


class _Detector:
    def __init__(self, clock, present_until):
        self.clock = clock
        self.present_until = present_until

    def poll(self, in_call=False):
        del in_call
        if self.clock.t < self.present_until:
            from meeting_notes.meet_detect import Sighting

            return Sighting(
                key="pitch-deck",
                display_name="Pitch Deck & Design Workshop",
                meet_code="",
                window_title="Meet - Pitch Deck & Design Workshop",
                source="google-meet",
            )
        return None


class _Lock:
    def current(self):
        return None


def _config():
    return Config(
        join_debounce_seconds=0,
        leave_debounce_seconds=0,
        poll_interval_seconds=1,
        outbox_retry_seconds=10**9,
        transcribe_timeout_seconds=30,
        watcher_transcript_only=False,
        speaker_labels=False,
        whisper_model="small",
        language="en",
        webhook_url="https://example.invalid/meeting-notes",
    )


def _write_audio(folder):
    os.makedirs(folder, exist_ok=True)
    for name in ("audio.wav", "audio-self.wav", "audio-others.wav"):
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(b"RIFFaudio-bytes")


def test_stopped_watcher_session_transcribes_summarizes_and_delivers_when_the_child_dies(
    tmp_path, monkeypatch
):
    """The recorder used to exit non-zero with only wavs left, and nothing retried."""
    monkeypatch.setattr(
        "meeting_notes.transcriber.transcribe",
        lambda path, model_size="medium", language="en": "[00:01] Pitch deck",
    )
    monkeypatch.setattr(
        "meeting_notes.summarizer.generate_notes",
        lambda text, name, date, model=None: "Summary\n- workshop",
    )
    delivered = []
    clock = _Clock()
    config = _config()

    class DyingChild:
        def __init__(self):
            self.started = []
            self.stopped = []
            self._state = "recording"

        def start(self, spec):
            self.started.append(spec)
            _write_audio(spec.folder)
            write_status(
                spec.folder,
                {
                    "state": "recording",
                    "mode": "watch",
                    "meeting_name": "Pitch Deck & Design Workshop",
                    "source": "google-meet",
                    "pid": 45400,
                    "whisper_model": "small",
                    "language": "en",
                },
            )
            return RunningSession(folder=spec.folder, pid=45400, break_ok=False)

        def request_stop(self, session):
            self.stopped.append((session.folder, session.break_ok))
            # Child dies after the wavs are closed, before Whisper publishes.
            self._state = "failed"

        def state(self, _session):
            return self._state

    launcher = DyingChild()

    def deliver(folder):
        delivered.append(folder)
        return True

    watcher = MeetWatcher(
        config=config,
        detector=_Detector(clock, present_until=2),
        launcher=launcher,
        deliver_fn=deliver,
        lock_store=_Lock(),
        outbox_retry=lambda: 0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-11-05T14:51:00-05:00",
        wall_datetime=lambda: __import__("datetime").datetime(
            2026, 11, 5, 14, 51, tzinfo=__import__("datetime").timezone.utc
        ),
        output_dir=str(tmp_path),
        log=_Log(),
    )
    watcher.run(max_ticks=4)

    assert len(launcher.started) == 1
    assert launcher.stopped == [(launcher.started[0].folder, False)]
    folder = launcher.started[0].folder
    assert transcript_ready(folder)
    assert (os.path.join(folder, "notes.md")) and os.path.isfile(os.path.join(folder, "notes.md"))
    notes = open(os.path.join(folder, "notes.md"), encoding="utf-8").read()
    assert "Summary" in notes or notes.strip()
    for name in ("audio.wav", "audio-self.wav", "audio-others.wav"):
        assert not os.path.exists(os.path.join(folder, name))
    assert delivered == [folder]
    status = read_status(folder)
    assert status["state"] == "done"
    assert status["delivered"] is True


def test_watcher_resumes_a_folder_with_audio_and_no_transcript(tmp_path, monkeypatch):
    folder = tmp_path / "2026-11-05_pitch-deck-design-workshop"
    _write_audio(str(folder))
    write_status(
        str(folder),
        {
            "state": "failed",
            "mode": "watch",
            "meeting_name": "Pitch Deck & Design Workshop",
            "source": "google-meet",
            "pid": 45400,
        },
    )
    monkeypatch.setattr(
        "meeting_notes.transcriber.transcribe",
        lambda path, model_size="medium", language="en": "[00:01] from disk",
    )
    monkeypatch.setattr(
        "meeting_notes.summarizer.generate_notes",
        lambda text, name, date, model=None: "Summary\n- resumed",
    )
    delivered = []
    clock = _Clock()
    watcher = MeetWatcher(
        config=_config(),
        detector=_Detector(clock, present_until=-1),
        launcher=DyingStub(),
        deliver_fn=lambda path: delivered.append(path) or True,
        lock_store=_Lock(),
        outbox_retry=lambda: 0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        wall_iso=lambda: "2026-11-05T15:55:00-05:00",
        wall_datetime=lambda: __import__("datetime").datetime(
            2026, 11, 5, 15, 55, tzinfo=__import__("datetime").timezone.utc
        ),
        output_dir=str(tmp_path),
        log=_Log(),
    )
    watcher.run(max_ticks=0)
    assert transcript_ready(str(folder))
    assert "resumed" in (folder / "notes.md").read_text(encoding="utf-8")
    assert not (folder / "audio.wav").exists()
    assert delivered == [str(folder)]


class DyingStub:
    def start(self, spec):
        raise AssertionError(spec)

    def request_stop(self, session):
        raise AssertionError(session)

    def state(self, _session):
        return "recording"


def test_process_command_delivers_watch_sessions_and_skips_manual(tmp_path, monkeypatch, capsys):
    meetings = tmp_path / "meetings"
    control = tmp_path / "control"
    meetings.mkdir()
    control.mkdir()
    monkeypatch.setattr(cli, "OUTPUT_DIR", str(meetings))
    monkeypatch.setattr(cli, "CONTROL_DIR", str(control))
    monkeypatch.setattr(
        "meeting_notes.transcriber.transcribe",
        lambda path, model_size="medium", language="en": "[00:01] workshop",
    )
    monkeypatch.setattr(
        "meeting_notes.summarizer.generate_notes",
        lambda text, name, date, model=None: "Summary\n- deck",
    )
    calls = []

    def deliver(folder, config, poster=None, sleep=None, outbox=None, control_dir=None, log=None):
        calls.append(folder)
        return True

    monkeypatch.setattr("meeting_notes.deliver.deliver_folder", deliver)

    manual = meetings / "2026-11-05_manual"
    _write_audio(str(manual))
    write_status(str(manual), {"state": "failed", "mode": "manual", "meeting_name": "Manual"})
    cli.cmd_process(cli.build_parser().parse_args(["process", str(manual)]))
    assert calls == []
    assert "not delivered" in capsys.readouterr().out
    assert transcript_ready(str(manual))
    assert not (manual / "audio.wav").exists()

    watched = meetings / "2026-11-05_pitch-deck-design-workshop"
    _write_audio(str(watched))
    write_status(
        str(watched),
        {
            "state": "failed",
            "mode": "watch",
            "meeting_name": "Pitch Deck & Design Workshop",
            "source": "google-meet",
        },
    )
    cli.cmd_process(cli.build_parser().parse_args(["process", str(watched)]))
    assert calls == [str(watched)]
    assert not (watched / "audio.wav").exists()
    assert "deck" in (watched / "notes.md").read_text(encoding="utf-8")


def test_finish_folder_keeps_audio_when_asked(tmp_path):
    folder = tmp_path / "session"
    _write_audio(str(folder))
    write_status(str(folder), {"state": "recording", "mode": "manual", "meeting_name": "Keep"})
    finish_folder(
        str(folder),
        Config(speaker_labels=False, watcher_transcript_only=False),
        transcript_only=True,
        keep_audio=True,
        deliver=False,
        transcribe_plain=lambda _path: "[00:01] kept",
    )
    assert (folder / "audio.wav").is_file()
    assert (folder / "audio-self.wav").is_file()
    assert transcript_ready(str(folder))
