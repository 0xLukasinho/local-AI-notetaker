import argparse
import os
import signal
import subprocess
import sys
import threading
from datetime import date

from meeting_notes.atomic_io import atomic_write_json, atomic_write_text
from meeting_notes.autostart import (
    AutostartError,
    autostart_status,
    install_autostart,
    uninstall_autostart,
)
from meeting_notes.call_sources import SOURCE_GOOGLE_MEET
from meeting_notes.config import Config, ConfigError, load_config
from meeting_notes.deliver import DeliveryError, deliver_folder
from meeting_notes.recorder import record_audio, stop_recording
from meeting_notes.session import (
    STATE_DONE,
    STATE_FAILED,
    STATE_TRANSCRIBING,
    allocate_meeting_dir,
    begin_session,
    clear_active,
    has_transcript,
    iso_now,
    live_session,
    pid_alive,
    read_active,
    read_watcher_pid,
    request_stop,
    slugify,
    LOG_FILENAME as WATCH_LOG_FILENAME,
    stop_requested,
    transcript_path as session_transcript_path,
    update_status,
)
from meeting_notes.summarizer import generate_notes
from meeting_notes.transcriber import transcribe
from meeting_notes.watcher import configure_watch_logging, serve
from meeting_notes.watch_guard import describe_heartbeat, ensure_until_running

OUTPUT_DIR = os.path.expanduser("~/meeting-notes")
CONTROL_DIR = os.path.expanduser("~/.meeting-notes")
DEFAULT_MODEL = os.environ.get("MEETING_NOTES_MODEL", "claude-opus-4-7")
DEFAULT_WHISPER_MODEL = "medium"
DEFAULT_LANGUAGE = "en"


def _hotkey_label():
    return "Cmd+Alt+Q" if sys.platform == "darwin" else "Ctrl+Alt+Q"


def _hotkey_combo():
    return "cmd+alt+q" if sys.platform == "darwin" else "ctrl+alt+q"


def copy_to_clipboard(text):
    """Copy text to the system clipboard. Returns True on success."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        if sys.platform == "win32":
            # Prefix with BOM so `clip` reads it as UTF-16 LE reliably.
            data = ("﻿" + text).encode("utf-16-le")
            subprocess.run(["clip"], input=data, check=True)
            return True
        return False
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def create_meeting_dir(meeting_name):
    """Create and return the meeting output directory path."""
    today = date.today().isoformat()
    slug = slugify(meeting_name)
    dir_name = f"{today}_{slug}"
    meeting_dir = os.path.join(OUTPUT_DIR, dir_name)
    os.makedirs(meeting_dir, exist_ok=True)
    return meeting_dir, today


def _install_stop_signal():
    """Wire up Ctrl+C and the global hotkey to a single stop Event.

    Returns the Event. The caller blocks on it (with periodic timeouts so
    SIGINT can be delivered on Windows during the wait). SIGBREAK is also
    accepted on Windows so CTRL_BREAK_EVENT can stop a process group.
    """
    stop_event = threading.Event()
    previous = {}

    def handle_sigint(_sig, _frame):
        stop_event.set()

    previous[signal.SIGINT] = signal.signal(signal.SIGINT, handle_sigint)
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        try:
            previous[signal.SIGBREAK] = signal.signal(signal.SIGBREAK, handle_sigint)
        except Exception:
            pass
    stop_event.previous_signals = previous

    hotkey_installed = False
    try:
        import keyboard  # type: ignore
        keyboard.add_hotkey(_hotkey_combo(), stop_event.set)
        hotkey_installed = True
    except Exception as e:
        print(
            f"Note: global hotkey unavailable ({e}). "
            "Press Ctrl+C in this terminal to stop.",
            file=sys.stderr,
        )

    if hotkey_installed:
        print(f"Press {_hotkey_label()} (or Ctrl+C) to stop recording.\n")
    else:
        print("Press Ctrl+C to stop recording.\n")

    return stop_event


def _restore_stop_signals(stop_event):
    previous = getattr(stop_event, "previous_signals", None) or {}
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def _wait_for_stop(stop_event, should_stop=None, poll_timeout=0.5):
    """Block until the stop event fires, polling so SIGINT can be serviced.

    `should_stop` is an optional callback polled alongside the event so a stop
    file written by `meeting-notes stop` (or another process) ends the
    recording without a console signal. The default poll is unchanged.
    """
    while not stop_event.is_set():
        if should_stop is not None and should_stop():
            stop_event.set()
            break
        stop_event.wait(timeout=poll_timeout)


def _release_hotkey():
    try:
        import keyboard  # type: ignore
        keyboard.unhook_all_hotkeys()
    except Exception:
        pass


def _maybe_delete_audio(audio_path, notes_path, keep_audio):
    """Delete the audio file by default; skip if keep_audio is True.

    Only deletes when notes.md exists and is non-empty.
    """
    if keep_audio:
        return
    if not os.path.exists(notes_path) or os.path.getsize(notes_path) == 0:
        return
    folder = os.path.dirname(audio_path)
    for name in ("audio.wav", "audio-self.wav", "audio-others.wav"):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
            print(f"Audio file deleted: {path}")
        except OSError as e:
            print(f"Warning: could not delete audio file: {e}", file=sys.stderr)


def _load_config_lenient():
    """Load config for manual commands. A missing file uses defaults.

    A broken config file must not change historical start/reprocess behavior,
    so it is reported and ignored.
    """
    try:
        return load_config(control_dir=CONTROL_DIR)
    except ConfigError as exc:
        print(
            f"Warning: could not read meeting-notes config ({exc}); using defaults.",
            file=sys.stderr,
        )
        return Config()


def _begin_recording(audio_path, separate_tracks):
    """Windows can write microphone and loopback as their own files.

    Other platforms, and test doubles that only accept a path, keep the
    single mixed recording.
    """
    if separate_tracks and sys.platform == "win32":
        return record_audio(audio_path, separate_tracks=True)
    return record_audio(audio_path)


def _uses_meet_speaker_bridge(source):
    """The Chrome extension only understands Google Meet.

    Manual recordings and Meet watch sessions keep it. Slack Huddles (and any
    later source) use the microphone / loopback split and do not collect Meet
    names.
    """
    return source in (None, "", SOURCE_GOOGLE_MEET)


def _start_speaker_bridge(meeting_dir, config):
    """Listen for the Meet extension while this recording is running."""
    if not getattr(config, "speaker_labels", False) or sys.platform != "win32":
        return None
    from meeting_notes.speaker_bridge import SpeakerBridge

    bridge = SpeakerBridge(
        os.path.join(meeting_dir, "speakers.jsonl"),
        port=getattr(config, "speaker_bridge_port", 8765),
    )
    try:
        bridge.start()
    except OSError as exc:
        print(
            f"Speaker bridge not started ({exc}). Remote speakers will be labeled Them.",
            file=sys.stderr,
        )
        return None
    print(
        f"Speaker bridge: 127.0.0.1:{bridge.bound_port} "
        "(optional Chrome extension; without it, other people are labeled Them)"
    )
    return bridge


def _stop_speaker_bridge(bridge):
    if bridge is None:
        return
    try:
        bridge.stop()
    except Exception:
        pass


def _write_transcript(meeting_dir, config, whisper_model, language):
    """Transcribe and, when speaker labels are possible, name each line."""
    from meeting_notes.speakers import TRANSCRIPT_JSON, produce_transcript
    from meeting_notes.transcriber import transcribe_paths

    def plain(path):
        return transcribe(path, model_size=whisper_model, language=language)

    def many(paths):
        return transcribe_paths(
            paths,
            model_size=whisper_model,
            language=language,
            word_timestamps=True,
        )

    text, document = produce_transcript(
        meeting_dir,
        self_name=getattr(config, "self_name", "You"),
        enabled=bool(getattr(config, "speaker_labels", True)),
        transcribe_plain=plain,
        transcribe_paths=many,
        diarize=bool(getattr(config, "diarize", False)),
        diarize_backend=getattr(config, "diarize_backend", "auto"),
        diarize_max_speakers=int(getattr(config, "diarize_max_speakers", 8) or 8),
        hf_token=getattr(config, "hf_token", "") or "",
    )
    atomic_write_text(session_transcript_path(meeting_dir), text)
    if document is not None:
        atomic_write_json(os.path.join(meeting_dir, TRANSCRIPT_JSON), document)
        names = ", ".join(document.get("participants") or [])
        source = document.get("speaker_source") or "tracks"
        print(f"Speakers ({source}): {names}")
        info = document.get("diarization") or {}
        if info.get("backend") and info.get("clusters"):
            print(f"Remote voices: {info['clusters']} via {info['backend']}.")
        elif info.get("reason"):
            print(f"Remote diarization skipped ({info['reason']}).")
        if document.get("captions") == "off":
            print(
                "Meet captions were off, so remote speakers could not be named "
                "from captions. Press CC in the Meet window next time. "
                "Captions are only shown to you."
            )
        update_status(
            meeting_dir,
            {
                "self_name": document.get("self_name"),
                "speaker_source": source,
                "participants": document.get("participants") or [],
            },
        )
    return text


def _wants_transcript_only(args, config):
    if getattr(args, "transcript_only", False) or getattr(args, "no_summary", False):
        return True
    return bool(config.transcript_only)


def _optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _refuse_second_session(conflict, automated):
    folder = conflict.get("folder")
    pid = conflict.get("pid")
    mode = conflict.get("mode")
    if automated:
        print(
            "Not starting a recording: a session is already running "
            f"(pid {pid}, mode {mode}, folder {folder}).",
            file=sys.stderr,
        )
        sys.exit(1)
    if mode == "watch":
        print(
            "Warning: a watcher recording is already in progress.\n"
            f"  Folder: {folder}\n"
            f"  PID: {pid}\n"
            "A second recording was not started. "
            "Stop the watcher session with `meeting-notes stop` "
            "if you want to record manually.",
            file=sys.stderr,
        )
    else:
        print(
            "Warning: another recording is already in progress.\n"
            f"  Folder: {folder}\n"
            f"  PID: {pid}\n"
            "A second recording was not started.",
            file=sys.stderr,
        )
    sys.exit(2)


def _mark_failed(folder, error):
    try:
        update_status(
            folder,
            {
                "state": STATE_FAILED,
                "error": error,
                "finished_at": iso_now(),
            },
        )
    except Exception:
        pass


def _resolve_folder(folder):
    if os.path.isabs(folder):
        return folder
    return os.path.join(OUTPUT_DIR, folder)


def cmd_start(args):
    """Handle the 'start' command: record, transcribe, summarize."""
    config = _load_config_lenient()
    transcript_only = _wants_transcript_only(args, config)
    automated = bool(getattr(args, "automated", False))
    meeting_name = args.name
    model = args.model or DEFAULT_MODEL
    whisper_model = args.whisper_model or DEFAULT_WHISPER_MODEL
    language = args.language or DEFAULT_LANGUAGE

    conflict = live_session(CONTROL_DIR, ignore_pid=os.getpid())
    if conflict is not None:
        _refuse_second_session(conflict, automated)

    output_dir = _optional_text(getattr(args, "output_dir", None))
    if output_dir:
        meeting_dir = os.path.abspath(output_dir)
        os.makedirs(meeting_dir, exist_ok=True)
        today = os.path.basename(meeting_dir).split("_", 1)[0]
    elif automated:
        meeting_dir = allocate_meeting_dir(OUTPUT_DIR, meeting_name, unique=True)
        today = os.path.basename(meeting_dir).split("_", 1)[0]
    else:
        meeting_dir, today = create_meeting_dir(meeting_name)

    audio_path = os.path.join(meeting_dir, "audio.wav")
    transcript_file = session_transcript_path(meeting_dir)
    notes_path = os.path.join(meeting_dir, "notes.md")

    if has_transcript(meeting_dir):
        print(
            f"Error: {transcript_file} already exists and will not be overwritten.\n"
            "Pick a different meeting name, or use `meeting-notes reprocess` "
            "to regenerate notes from this session.",
            file=sys.stderr,
        )
        sys.exit(1)

    mode = "watch" if automated else "manual"
    source = _optional_text(getattr(args, "source", None))
    begin_session(
        meeting_dir,
        CONTROL_DIR,
        mode=mode,
        transcript_only=transcript_only,
        meeting_name=meeting_name,
        whisper_model=whisper_model,
        language=language,
        meet_code=_optional_text(getattr(args, "meet_code", None)),
        window_title=_optional_text(getattr(args, "window_title", None)),
        joined_at=_optional_text(getattr(args, "joined_at", None)),
        break_ok=automated and sys.platform == "win32",
        honor_existing_stop=bool(automated or output_dir),
        source=source,
    )

    # --- Recording ---
    print(f"Recording meeting: {meeting_name}")
    print(f"Output directory: {meeting_dir}")

    stop_event = None
    handle = None
    bridge = None
    try:
        stop_event = _install_stop_signal()
        bridge = None
        if _uses_meet_speaker_bridge(source):
            bridge = _start_speaker_bridge(meeting_dir, config)
        try:
            handle = _begin_recording(audio_path, bool(config.speaker_labels))
        except SystemExit:
            _mark_failed(meeting_dir, "recording failed to start")
            raise
        if bridge is not None:
            bridge.mark_audio_start()

        try:
            _wait_for_stop(
                stop_event,
                should_stop=lambda: stop_requested(meeting_dir, CONTROL_DIR),
            )
        finally:
            print("\nStopping recording...")
            if handle is not None:
                try:
                    stop_recording(handle)
                except Exception as e:
                    print(f"Recorder error: {e}", file=sys.stderr)
            _release_hotkey()
            _stop_speaker_bridge(bridge)
            bridge = None

        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            _mark_failed(meeting_dir, "No audio was recorded")
            print(
                "Error: No audio was recorded. Check your audio device settings.",
                file=sys.stderr,
            )
            sys.exit(1)

        # --- Transcription ---
        stopped_at = iso_now()
        update_status(
            meeting_dir,
            {
                "state": STATE_TRANSCRIBING,
                "recording_stopped_at": stopped_at,
                "transcription_started_at": stopped_at,
                "left_at": stopped_at,
            },
        )
        print("\n--- Transcription ---")
        transcript = _write_transcript(meeting_dir, config, whisper_model, language)
        print(f"Transcript saved: {transcript_file}")

        if transcript_only:
            print("Transcript-only mode: skipping note generation.")
            update_status(
                meeting_dir,
                {
                    "state": STATE_DONE,
                    "finished_at": iso_now(),
                    "error": None,
                },
            )
            print("Done!")
            return

        # --- Note Generation ---
        print("\n--- Note Generation ---")
        notes = generate_notes(transcript, meeting_name, today, model=model)

        if notes is None:
            _mark_failed(meeting_dir, "Failed to generate notes")
            print("Failed to generate notes. Transcript was saved.", file=sys.stderr)
            sys.exit(1)

        with open(notes_path, "w", encoding="utf-8") as f:
            f.write(notes)

        print(f"\nNotes saved: {notes_path}")
        if copy_to_clipboard(notes):
            print("Notes copied to clipboard — paste into Notion with Ctrl+V.")

        _maybe_delete_audio(audio_path, notes_path, args.keep_audio)
        update_status(
            meeting_dir,
            {
                "state": STATE_DONE,
                "finished_at": iso_now(),
                "error": None,
            },
        )
        print("Done!")
    except Exception as exc:
        _mark_failed(meeting_dir, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _stop_speaker_bridge(bridge)
        if stop_event is not None:
            _restore_stop_signals(stop_event)
        clear_active(CONTROL_DIR, pid=os.getpid())


def cmd_reprocess(args):
    """Re-run transcription and/or summarization on an existing meeting."""
    config = _load_config_lenient()
    explicit_skip = bool(args.transcript_only or args.no_summary)
    notes_only = args.notes_only
    if notes_only and explicit_skip:
        print(
            "Error: --transcript-only cannot be combined with --notes-only.",
            file=sys.stderr,
        )
        sys.exit(2)
    # --notes-only is an explicit request for Claude notes, including when the
    # config file turns transcript-only on for ordinary start/reprocess runs.
    transcript_only = False if notes_only else _wants_transcript_only(args, config)

    model = args.model or DEFAULT_MODEL
    whisper_model = args.whisper_model or DEFAULT_WHISPER_MODEL
    language = args.language or DEFAULT_LANGUAGE

    meeting_dir = _resolve_folder(args.folder)

    if not os.path.isdir(meeting_dir):
        print(f"Error: Meeting folder not found: {meeting_dir}", file=sys.stderr)
        print("Use 'meeting-notes list' to see available meetings.", file=sys.stderr)
        sys.exit(1)

    audio_path = os.path.join(meeting_dir, "audio.wav")
    transcript_file = session_transcript_path(meeting_dir)
    notes_path = os.path.join(meeting_dir, "notes.md")

    folder_name = os.path.basename(meeting_dir)
    parts = folder_name.split("_", 1)
    meeting_date = parts[0] if len(parts) > 0 else "unknown"
    meeting_name = parts[1].replace("-", " ").title() if len(parts) > 1 else folder_name

    if notes_only:
        if not os.path.exists(transcript_file):
            print(f"Error: No transcript found at {transcript_file}", file=sys.stderr)
            sys.exit(1)

        with open(transcript_file, "r", encoding="utf-8") as f:
            transcript = f.read()

        print(f"Reprocessing notes for: {meeting_name}")
        print("\n--- Note Generation ---")
        notes = generate_notes(transcript, meeting_name, meeting_date, model=model)
    elif transcript_only:
        if not os.path.exists(audio_path):
            print(f"Error: No audio file found at {audio_path}", file=sys.stderr)
            sys.exit(1)

        print(f"Reprocessing: {meeting_name}")
        print("\n--- Transcription ---")
        update_status(
            meeting_dir,
            {
                "state": STATE_TRANSCRIBING,
                "transcription_started_at": iso_now(),
                "whisper_model": whisper_model,
                "language": language,
                "transcript_only": True,
            },
        )
        transcript = _write_transcript(meeting_dir, config, whisper_model, language)
        print(f"Transcript saved: {transcript_file}")
        print("Transcript-only mode: skipping note generation.")
        update_status(
            meeting_dir,
            {
                "state": STATE_DONE,
                "finished_at": iso_now(),
                "error": None,
                "transcript_only": True,
            },
        )
        print("Done!")
        return
    else:
        if not os.path.exists(audio_path):
            print(f"Error: No audio file found at {audio_path}", file=sys.stderr)
            sys.exit(1)

        print(f"Reprocessing: {meeting_name}")

        print("\n--- Transcription ---")
        transcript = _write_transcript(meeting_dir, config, whisper_model, language)
        print(f"Transcript saved: {transcript_file}")

        print("\n--- Note Generation ---")
        notes = generate_notes(transcript, meeting_name, meeting_date, model=model)

    if notes is None:
        _mark_failed(meeting_dir, "Failed to generate notes")
        print("Failed to generate notes.", file=sys.stderr)
        sys.exit(1)

    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(notes)

    print(f"\nNotes saved: {notes_path}")
    if copy_to_clipboard(notes):
        print("Notes copied to clipboard — paste into Notion with Ctrl+V.")

    _maybe_delete_audio(audio_path, notes_path, args.keep_audio)
    update_status(
        meeting_dir,
        {
            "state": STATE_DONE,
            "finished_at": iso_now(),
            "error": None,
            "transcript_only": False,
        },
    )
    print("Done!")


def cmd_list(_args):
    """List past meetings."""
    from meeting_notes.library import list_meetings

    meetings = list_meetings(OUTPUT_DIR)
    if not meetings:
        print("No meetings found.")
        return

    print("Past meetings:\n")
    for m in meetings:
        status = "notes ready" if m.has_notes else "no notes"
        print(f"  {m.date}  {m.title}  ({status})")


def cmd_stop(_args):
    """Ask the running recorder to flush audio and transcribe."""
    active = live_session(CONTROL_DIR)
    if active is None:
        stale = read_active(CONTROL_DIR)
        if stale is not None and not pid_alive(stale.get("pid")):
            clear_active(CONTROL_DIR, pid=stale.get("pid"))
        print("No recording session is running.", file=sys.stderr)
        sys.exit(1)
    folder = active.get("folder")
    pid = active.get("pid")
    if not folder:
        print("No recording session is running.", file=sys.stderr)
        sys.exit(1)
    sent = request_stop(CONTROL_DIR, folder, pid, break_ok=bool(active.get("break_ok")))
    print(f"Stop requested for {folder} (pid {pid}).")
    if sent:
        print("Sent CTRL_BREAK_EVENT to the recorder process group.")
    else:
        print("Signaled via stop file.")


class _DeliverLog:
    """Surface delivery progress on the console without the webhook credential."""

    def info(self, message, *args):
        print(message % args if args else message)

    def error(self, message, *args):
        print(message % args if args else message, file=sys.stderr)


def cmd_deliver(args):
    """POST a finished session's transcript to the configured webhook."""
    try:
        config = load_config(control_dir=CONTROL_DIR)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    meeting_dir = _resolve_folder(args.folder)
    if not os.path.isdir(meeting_dir):
        print(f"Error: Meeting folder not found: {meeting_dir}", file=sys.stderr)
        sys.exit(1)
    try:
        ok = deliver_folder(
            meeting_dir,
            config,
            control_dir=CONTROL_DIR,
            log=_DeliverLog(),
        )
    except DeliveryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if not ok:
        if not config.webhook_url:
            print(
                "Error: webhook_url is not configured. Set it in "
                "~/.meeting-notes/config.toml or MEETING_NOTES_WEBHOOK_URL.",
                file=sys.stderr,
            )
        else:
            print(
                "Error: delivery failed; the payload was saved to the outbox "
                "and will be retried by `meeting-notes watch`.",
                file=sys.stderr,
            )
        sys.exit(1)
    print(f"Delivered {meeting_dir}")


def _config_status_line():
    toml_path = os.path.join(CONTROL_DIR, "config.toml")
    json_path = os.path.join(CONTROL_DIR, "config.json")
    if os.path.exists(toml_path):
        config_path, found = toml_path, True
    elif os.path.exists(json_path):
        config_path, found = json_path, True
    else:
        config_path, found = toml_path, False
    return f"Config: {config_path} ({'found' if found else 'not found, using defaults'})"


def _print_watch_status():
    status = autostart_status(CONTROL_DIR)
    pid = read_watcher_pid(CONTROL_DIR)
    running = bool(pid) and pid_alive(pid)
    installed = "installed" if status["installed"] else "not installed"
    print(f"Autostart: {installed} ({status['detail']})")
    if running:
        print(f"Watcher process: running (pid {pid})")
    else:
        print("Watcher process: not running")
    print(describe_heartbeat(CONTROL_DIR))
    print(_config_status_line())
    print(f"Log: {os.path.join(CONTROL_DIR, 'watch.log')}")


def _log_watch(message, *args):
    try:
        logger, _path = configure_watch_logging(CONTROL_DIR)
        logger.error(message, *args)
    except Exception:
        pass


def _print_autostart_result(meta):
    removed = meta.get("removed_startup") or []
    if removed:
        print("Removed the old Startup-folder shortcut:")
        for path in removed:
            print(f"  {path}")
        print("The scheduled task replaces it.")
    print(f"Installed scheduled task {meta.get('task_name')}.")
    if meta.get("startup_trigger"):
        print("It runs hidden at system startup and at logon.")
    else:
        print("It runs hidden at logon.")
        print(
            "System startup was skipped because registering it needs an "
            "Administrator PowerShell. Logon still runs it. Rerun "
            "`meeting-notes autostart install` from an Administrator PowerShell "
            "to add the startup trigger."
        )
        detail = meta.get("startup_error") or ""
        if detail and "access is denied" not in detail.lower() and "Administrator PowerShell" not in detail:
            print(f"  {detail}")
    print("It also runs when the session unlocks.")
    if meta.get("resume_trigger"):
        print("It also runs on resume from sleep.")
    else:
        print("Resume-from-sleep could not be registered. Logon and unlock still run.")
        if meta.get("resume_error"):
            print(f"  {meta['resume_error']}")
    print("Each run starts a hidden watcher only when one is not already healthy.")
    print("If that start fails, it retries after 30 seconds, 60 seconds, and 120 seconds.")


def _cmd_install_autostart():
    if sys.platform != "win32":
        print("Autostart install is supported on Windows only.", file=sys.stderr)
        sys.exit(1)
    try:
        meta = install_autostart(CONTROL_DIR)
    except AutostartError as exc:
        _log_watch("autostart install failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    _print_autostart_result(meta)
    try:
        logger, _path = configure_watch_logging(CONTROL_DIR)
        logger.info(
            "autostart install: task %s triggers %s",
            meta.get("task_name"),
            ", ".join(meta.get("triggers") or []),
        )
    except Exception:
        pass
    result = ensure_until_running(CONTROL_DIR)
    print(result.user_message)
    if not result.ok:
        sys.exit(1)


def _cmd_uninstall_autostart():
    removed = uninstall_autostart(CONTROL_DIR)
    if removed:
        print("Removed login autostart:")
        for item in removed:
            print(f"  {item}")
    else:
        print("Login autostart was not installed.")
    try:
        logger, _path = configure_watch_logging(CONTROL_DIR)
        logger.info("autostart uninstall: removed %s", ", ".join(removed) or "nothing")
    except Exception:
        pass


def cmd_autostart(args):
    """Install, remove, or inspect the Windows scheduled task."""
    command = args.autostart_command
    if command == "install":
        _cmd_install_autostart()
        return
    if command == "uninstall":
        _cmd_uninstall_autostart()
        return
    if command == "status":
        _print_watch_status()
        return
    print("Error: expected install, uninstall, or status.", file=sys.stderr)
    sys.exit(2)


def cmd_speakers(args):
    """Copy the unpacked Meet extension and print the one-time Chrome steps."""
    from meeting_notes.extension_install import install_extension

    config = _load_config_lenient()
    try:
        folder = install_extension(CONTROL_DIR, config.speaker_bridge_port)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.path:
        print(folder)
        return
    print(f"Copied the Meet speaker extension to:\n  {folder}\n")
    print("One-time setup in Chrome:")
    print("  1. Open chrome://extensions")
    print("  2. Turn on Developer mode")
    print("  3. Click Load unpacked")
    print("  4. Choose the folder above")
    print()
    print(
        "The extension reads participant names and, if you turn captions on,"
    )
    print("the caption speaker on meet.google.com, and sends them")
    print(
        f"to 127.0.0.1:{config.speaker_bridge_port} on this computer while a recording"
    )
    print("is running. Other people in the call cannot see it.")
    print("Turn on captions with the CC button in Meet. That is what names")
    print("each remote speaker. Captions are only shown to you.")
    print("Run this command again after you change speaker_bridge_port, then click")
    print("Reload on the extension.")
    print("Recording works without the extension: you are self_name, everyone else is Them.")


def cmd_watch(args):
    """Detect Google Meet calls and record transcript-only sessions."""
    selected = sum(
        bool(flag) for flag in (args.install, args.uninstall, args.status, args.ensure)
    )
    if selected > 1:
        print(
            "Error: pass only one of --ensure, --install, --uninstall, or --status.",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.status:
        _print_watch_status()
        return
    if args.uninstall:
        _cmd_uninstall_autostart()
        return
    if args.install:
        _cmd_install_autostart()
        return
    if args.ensure:
        if sys.platform != "win32":
            print(
                "meeting-notes watch --ensure requires Windows.",
                file=sys.stderr,
            )
            sys.exit(1)
        result = ensure_until_running(CONTROL_DIR)
        print(result.user_message)
        sys.exit(0 if result.ok else 1)
    if sys.platform != "win32":
        print(
            "meeting-notes watch requires Windows "
            "(Chrome window enumeration and the microphone probes).",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        config = load_config(control_dir=CONTROL_DIR)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    log_path = os.path.join(CONTROL_DIR, WATCH_LOG_FILENAME)
    sources = []
    if config.watch_google_meet:
        sources.append("Google Meet")
    if config.watch_slack_huddle:
        sources.append("Slack Huddle")
    watching = " and ".join(sources) if sources else "no call sources"
    print(f"Watching for {watching}. Log: {log_path}")
    print("Press Ctrl+C to stop the watcher.")
    try:
        serve(config, CONTROL_DIR, OUTPUT_DIR)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _add_transcript_flags(parser):
    parser.add_argument(
        "--transcript-only",
        action="store_true",
        help="Record and transcribe only; do not call claude or write notes.md",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Alias for --transcript-only",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="meeting-notes",
        description="Privacy-first local meeting notes generator (Mac + Windows)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    start_parser = subparsers.add_parser("start", help="Record and process a meeting")
    start_parser.add_argument("name", help="Meeting name")
    start_parser.add_argument(
        "--model", help=f"Claude model for note generation (default: {DEFAULT_MODEL})"
    )
    start_parser.add_argument(
        "--whisper-model",
        help=f"Whisper model size (default: {DEFAULT_WHISPER_MODEL})",
        choices=["tiny", "base", "small", "medium", "large"],
    )
    start_parser.add_argument(
        "--language",
        help=f"Language code to force, or 'auto' to detect (default: {DEFAULT_LANGUAGE})",
    )
    start_parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Keep audio.wav on disk (default: deleted after notes are generated)",
    )
    _add_transcript_flags(start_parser)
    start_parser.add_argument(
        "--automated",
        action="store_true",
        help="Unique session folder and mode=watch (used by `meeting-notes watch`)",
    )
    start_parser.add_argument(
        "--output-dir",
        help="Write into this session directory instead of allocating one",
    )
    start_parser.add_argument("--meet-code", help="Google Meet code, stored in status.json")
    start_parser.add_argument("--window-title", help="Browser window title, stored in status.json")
    start_parser.add_argument(
        "--joined-at",
        help="ISO 8601 join time with offset, stored in status.json",
    )
    start_parser.add_argument(
        "--source",
        help="Call source stored in status.json (used by meeting-notes watch)",
    )
    start_parser.set_defaults(func=cmd_start)

    # reprocess
    reprocess_parser = subparsers.add_parser(
        "reprocess",
        help="Re-run transcription and note generation on an existing meeting",
    )
    reprocess_parser.add_argument("folder", help="Meeting folder name (from 'meeting-notes list')")
    reprocess_parser.add_argument(
        "--model", help=f"Claude model for note generation (default: {DEFAULT_MODEL})"
    )
    reprocess_parser.add_argument(
        "--whisper-model",
        help=f"Whisper model size (default: {DEFAULT_WHISPER_MODEL})",
        choices=["tiny", "base", "small", "medium", "large"],
    )
    reprocess_parser.add_argument(
        "--language",
        help=f"Language code to force, or 'auto' to detect (default: {DEFAULT_LANGUAGE})",
    )
    reprocess_parser.add_argument(
        "--notes-only",
        action="store_true",
        help="Only regenerate notes from existing transcript (skip transcription)",
    )
    reprocess_parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Keep audio.wav on disk (default: deleted after notes are generated)",
    )
    _add_transcript_flags(reprocess_parser)
    reprocess_parser.set_defaults(func=cmd_reprocess)

    # list
    list_parser = subparsers.add_parser("list", help="List past meetings")
    list_parser.set_defaults(func=cmd_list)

    stop_parser = subparsers.add_parser(
        "stop",
        help="Gracefully stop the running recording (flush audio, then transcribe)",
    )
    stop_parser.set_defaults(func=cmd_stop)

    deliver_parser = subparsers.add_parser(
        "deliver",
        help="POST a finished session transcript to the configured webhook",
    )
    deliver_parser.add_argument("folder", help="Meeting folder name or absolute path")
    deliver_parser.set_defaults(func=cmd_deliver)

    watch_parser = subparsers.add_parser(
        "watch",
        help="Watch for a Google Meet or Slack Huddle and record a transcript",
    )
    watch_parser.add_argument(
        "--ensure",
        action="store_true",
        help=(
            "If no healthy watcher is running, start one hidden and exit. "
            "A healthy watcher makes this return immediately."
        ),
    )
    watch_parser.add_argument(
        "--install",
        action="store_true",
        help="Same as `meeting-notes autostart install`",
    )
    watch_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Same as `meeting-notes autostart uninstall`",
    )
    watch_parser.add_argument(
        "--status",
        action="store_true",
        help="Show the scheduled task, the watcher process, and the heartbeat",
    )
    watch_parser.set_defaults(func=cmd_watch)

    autostart_parser = subparsers.add_parser(
        "autostart",
        help="Start the watcher at logon, unlock, and resume (Windows)",
    )
    autostart_subs = autostart_parser.add_subparsers(dest="autostart_command", required=True)
    autostart_subs.add_parser(
        "install",
        help="Register a per-user scheduled task that runs watch --ensure",
    )
    autostart_subs.add_parser(
        "uninstall",
        help="Remove the scheduled task and any Startup-folder shortcut",
    )
    autostart_subs.add_parser(
        "status",
        help="Show the scheduled task, the watcher process, and the heartbeat",
    )
    autostart_parser.set_defaults(func=cmd_autostart)

    speakers_parser = subparsers.add_parser(
        "speakers",
        help="Install the Chrome extension that reads Google Meet speaker names",
    )
    speakers_parser.add_argument(
        "--path",
        action="store_true",
        help="Print the extension folder and do not print the setup steps",
    )
    speakers_parser.set_defaults(func=cmd_speakers)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
