import argparse
import os
import signal
import subprocess
import sys
import threading
from datetime import date

from meeting_notes.atomic_io import atomic_write_text
from meeting_notes.autostart import autostart_status, install_autostart, uninstall_autostart
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
from meeting_notes.watcher import serve

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
    if not os.path.exists(audio_path):
        return
    try:
        os.remove(audio_path)
        print(f"Audio file deleted: {audio_path}")
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
    )

    # --- Recording ---
    print(f"Recording meeting: {meeting_name}")
    print(f"Output directory: {meeting_dir}")

    stop_event = None
    handle = None
    try:
        stop_event = _install_stop_signal()
        try:
            handle = record_audio(audio_path)
        except SystemExit:
            _mark_failed(meeting_dir, "recording failed to start")
            raise

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
        transcript = transcribe(audio_path, model_size=whisper_model, language=language)

        atomic_write_text(transcript_file, transcript)
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
        transcript = transcribe(audio_path, model_size=whisper_model, language=language)
        atomic_write_text(transcript_file, transcript)
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
        transcript = transcribe(audio_path, model_size=whisper_model, language=language)

        atomic_write_text(transcript_file, transcript)
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
        ok = deliver_folder(meeting_dir, config, control_dir=CONTROL_DIR)
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


def _print_watch_status():
    status = autostart_status(CONTROL_DIR)
    pid = read_watcher_pid(CONTROL_DIR)
    running = bool(pid) and pid_alive(pid)
    toml_path = os.path.join(CONTROL_DIR, "config.toml")
    json_path = os.path.join(CONTROL_DIR, "config.json")
    if os.path.exists(toml_path):
        config_path, found = toml_path, True
    elif os.path.exists(json_path):
        config_path, found = json_path, True
    else:
        config_path, found = toml_path, False
    installed = "installed" if status["installed"] else "not installed"
    print(f"Autostart: {installed} ({status['detail']})")
    if running:
        print(f"Watcher process: running (pid {pid})")
    else:
        print("Watcher process: not running")
    print(f"Config: {config_path} ({'found' if found else 'not found, using defaults'})")
    print(f"Log: {os.path.join(CONTROL_DIR, 'watch.log')}")


def cmd_watch(args):
    """Detect Google Meet calls and record transcript-only sessions."""
    selected = sum(bool(flag) for flag in (args.install, args.uninstall, args.status))
    if selected > 1:
        print("Error: pass only one of --install, --uninstall, or --status.", file=sys.stderr)
        sys.exit(2)
    if args.status:
        _print_watch_status()
        return
    if args.uninstall:
        removed = uninstall_autostart(CONTROL_DIR)
        if removed:
            print("Removed login autostart:")
            for item in removed:
                print(f"  {item}")
        else:
            print("Login autostart was not installed.")
        return
    if args.install:
        if sys.platform != "win32":
            print("Autostart install is supported on Windows only.", file=sys.stderr)
            sys.exit(1)
        meta = install_autostart(CONTROL_DIR)
        method = meta.get("method")
        if method == "schtasks":
            print(f"Installed Task Scheduler task {meta.get('task_name')}.")
        else:
            print(f"Installed Startup script: {meta.get('path')}")
        print("It will start at the next login. Run `meeting-notes watch` to start it now.")
        return
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
    print(f"Watching for Google Meet calls. Log: {log_path}")
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
        help="Watch for a Google Meet call in Chrome and record a transcript",
    )
    watch_parser.add_argument(
        "--install",
        action="store_true",
        help="Run the watcher at Windows login (Task Scheduler, else Startup script)",
    )
    watch_parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the login autostart entry",
    )
    watch_parser.add_argument(
        "--status",
        action="store_true",
        help="Show autostart and whether the watcher process is running",
    )
    watch_parser.set_defaults(func=cmd_watch)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
