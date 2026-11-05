"""Transcribe, summarize, and deliver one session folder.

The recorder child used to be the only process that could finish a meeting.
If it died after the wavs were closed and before ``transcript.txt`` was
published (CTRL_BREAK, ``taskkill /T``, or a reboot), the watcher saw a
non-zero exit, logged ``failed``, and left the audio in place. This module
is the one finish path for that child, for ``meeting-notes process``, and
for the watcher when it finds a folder that still has audio and no transcript.
"""

import os

from meeting_notes.session import (
    iso_now,
    read_status,
    transcript_path,
    update_status,
    STATE_DONE,
    STATE_FAILED,
    STATE_TRANSCRIBING,
)

AUDIO_NAMES = ("audio.wav", "audio-self.wav", "audio-others.wav")


class SessionFinishError(RuntimeError):
    """The folder could not be transcribed or summarized."""


def transcript_ready(folder):
    """True when transcript.txt exists and is not empty."""
    path = transcript_path(folder)
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def folder_has_audio(folder):
    """True when a non-empty recording is still on disk."""
    for name in AUDIO_NAMES:
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return True
        except OSError:
            continue
    return False


def delete_session_audio(folder):
    """Remove the recording files. Returns (paths, bytes_freed)."""
    removed = []
    freed = 0
    for name in AUDIO_NAMES:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            continue
        removed.append(path)
        freed += size
    return removed, freed


def session_should_deliver(folder):
    """Watcher sessions are delivered. Manual sessions are not."""
    status = read_status(folder) or {}
    return status.get("mode") == "watch"


def _meeting_identity(folder, status):
    status = status or {}
    name = (status.get("meeting_name") or "").strip()
    base = os.path.basename(os.path.abspath(folder))
    parts = base.split("_", 1)
    meeting_date = parts[0] if parts and parts[0] else "unknown"
    if not name:
        name = parts[1].replace("-", " ").title() if len(parts) > 1 else base
    return name, meeting_date


def _say(log, message):
    if log is None:
        print(message)
        return
    info = getattr(log, "info", None)
    if info is None:
        print(message)
        return
    info("%s", message)


def _fail(log, message):
    if log is None:
        return
    error = getattr(log, "error", None)
    if error is not None:
        error("%s", message)


def write_transcript(folder, config, whisper_model, language, transcribe_plain=None, transcribe_paths=None):
    """Write transcript.txt. Returns the text. Does not delete audio."""
    from meeting_notes.atomic_io import atomic_write_json, atomic_write_text
    from meeting_notes.speakers import TRANSCRIPT_JSON, produce_transcript
    from meeting_notes.transcriber import transcribe, transcribe_paths as default_paths

    model = whisper_model or getattr(config, "whisper_model", None) or "medium"
    lang = language or getattr(config, "language", None) or "en"

    def plain(path):
        if transcribe_plain is not None:
            return transcribe_plain(path)
        return transcribe(path, model_size=model, language=lang)

    def many(paths):
        if transcribe_paths is not None:
            return transcribe_paths(paths)
        return default_paths(
            paths,
            model_size=model,
            language=lang,
            word_timestamps=True,
        )

    text, document = produce_transcript(
        folder,
        self_name=getattr(config, "self_name", "You"),
        enabled=bool(getattr(config, "speaker_labels", True)),
        transcribe_plain=plain,
        transcribe_paths=many,
        diarize=bool(getattr(config, "diarize", False)),
        diarize_backend=getattr(config, "diarize_backend", "auto"),
        diarize_max_speakers=int(getattr(config, "diarize_max_speakers", 8) or 8),
        hf_token=getattr(config, "hf_token", "") or "",
    )
    atomic_write_text(transcript_path(folder), text if text is not None else "")
    if document is not None:
        atomic_write_json(os.path.join(folder, TRANSCRIPT_JSON), document)
        update_status(
            folder,
            {
                "self_name": document.get("self_name"),
                "speaker_source": document.get("speaker_source") or "tracks",
                "participants": document.get("participants") or [],
            },
        )
    return text or ""


def finish_folder(
    folder,
    config,
    *,
    meeting_name=None,
    meeting_date=None,
    model=None,
    whisper_model=None,
    language=None,
    transcript_only=None,
    keep_audio=None,
    deliver=None,
    control_dir=None,
    log=None,
    transcribe_plain=None,
    transcribe_paths=None,
    notes_fn=None,
    deliver_fn=None,
):
    """Transcribe if needed, summarize, delete audio, and maybe deliver.

    ``deliver=None`` delivers only when status.json says ``mode`` is ``watch``.
    A manual session is never delivered. Audio is removed only after a
    non-empty transcript is on disk, and only when keep-audio is off.
    """
    folder = os.path.abspath(folder)
    status = read_status(folder) or {}
    if meeting_name is None or meeting_date is None:
        inferred_name, inferred_date = _meeting_identity(folder, status)
        meeting_name = meeting_name or inferred_name
        meeting_date = meeting_date or inferred_date
    if transcript_only is None:
        if status.get("mode") == "watch":
            transcript_only = bool(getattr(config, "watcher_transcript_only", False))
        else:
            transcript_only = bool(status.get("transcript_only")) or bool(
                getattr(config, "transcript_only", False)
            )
    if keep_audio is None:
        keep_audio = bool(getattr(config, "keep_audio", False))
    if deliver is None:
        deliver = session_should_deliver(folder)
    whisper_model = whisper_model or status.get("whisper_model") or getattr(config, "whisper_model", "medium")
    language = language or status.get("language") or getattr(config, "language", "en")

    if not transcript_ready(folder):
        if not folder_has_audio(folder):
            raise SessionFinishError(f"no audio to transcribe in {folder}")
        update_status(
            folder,
            {
                "state": STATE_TRANSCRIBING,
                "transcription_started_at": iso_now(),
                "whisper_model": whisper_model,
                "language": language,
            },
        )
        _say(log, "\n--- Transcription ---")
        try:
            text = write_transcript(
                folder,
                config,
                whisper_model,
                language,
                transcribe_plain=transcribe_plain,
                transcribe_paths=transcribe_paths,
            )
        except Exception as exc:
            update_status(
                folder,
                {
                    "state": STATE_FAILED,
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": iso_now(),
                },
            )
            raise
        _say(log, f"Transcript saved: {transcript_path(folder)}")
    else:
        with open(transcript_path(folder), "r", encoding="utf-8") as handle:
            text = handle.read()

    removed = []
    if transcript_ready(folder) and not keep_audio:
        removed, _freed = delete_session_audio(folder)
        for path in removed:
            _say(log, f"Audio file deleted: {path}")

    notes_path = os.path.join(folder, "notes.md")
    notes = None
    if transcript_only:
        _say(log, "Transcript-only mode: skipping note generation.")
    else:
        existing_notes = ""
        if os.path.isfile(notes_path):
            try:
                with open(notes_path, "r", encoding="utf-8") as handle:
                    existing_notes = handle.read()
            except OSError:
                existing_notes = ""
        if existing_notes.strip():
            notes = existing_notes
        else:
            _say(log, "\n--- Note Generation ---")
            generate = notes_fn
            if generate is None:
                from meeting_notes.summarizer import generate_notes

                generate = generate_notes
            try:
                notes = generate(text, meeting_name, meeting_date, model=model)
            except TypeError:
                notes = generate(text, meeting_name, meeting_date)
            if not notes:
                update_status(
                    folder,
                    {
                        "state": STATE_FAILED,
                        "error": "Failed to generate notes",
                        "finished_at": iso_now(),
                    },
                )
                _fail(log, "Failed to generate notes. Transcript was saved.")
                raise SessionFinishError("Failed to generate notes")
            with open(notes_path, "w", encoding="utf-8") as handle:
                handle.write(notes)
            _say(log, f"\nNotes saved: {notes_path}")

    delivered = bool((read_status(folder) or {}).get("delivered"))
    if deliver and not delivered:
        sender = deliver_fn
        if sender is None:
            from meeting_notes.deliver import deliver_folder

            def sender(path):
                return deliver_folder(path, config, control_dir=control_dir, log=log)

        try:
            delivered = bool(sender(folder))
        except Exception as exc:
            _fail(log, f"Delivery failed for {folder}: {exc}")
            delivered = False
        if delivered and not (read_status(folder) or {}).get("delivered"):
            update_status(
                folder,
                {"delivered": True, "delivered_at": iso_now(), "delivery_error": None},
            )

    update_status(
        folder,
        {
            "state": STATE_DONE,
            "finished_at": iso_now(),
            "error": None,
            "transcript_only": bool(transcript_only),
        },
    )
    _say(log, "Done!")
    return {
        "transcript": text,
        "notes": notes,
        "delivered": delivered,
        "deleted_audio": removed,
    }


def unfinished_folders(output_dir, skip_folder=None):
    """Session folders that still need transcription or watcher delivery.

    A folder with audio and no transcript is unfinished. A watcher folder
    whose transcript was written but never delivered is unfinished too.
    ``skip_folder`` is the recording that is still open.
    """
    if not output_dir or not os.path.isdir(output_dir):
        return []
    skip = os.path.abspath(skip_folder) if skip_folder else None
    found = []
    for name in sorted(os.listdir(output_dir)):
        folder = os.path.join(output_dir, name)
        if not os.path.isdir(folder):
            continue
        if skip and os.path.abspath(folder) == skip:
            continue
        status = read_status(folder) or {}
        if status.get("state") == "recording":
            from meeting_notes.session import pid_alive

            if pid_alive(status.get("pid")):
                continue
        ready = transcript_ready(folder)
        if not ready and folder_has_audio(folder):
            found.append(folder)
            continue
        if ready and status.get("mode") == "watch" and not status.get("delivered"):
            found.append(folder)
    return found
