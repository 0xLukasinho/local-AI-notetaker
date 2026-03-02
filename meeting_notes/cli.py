import argparse
import os
import re
import signal
import subprocess
import sys
from datetime import date

from meeting_notes.recorder import record_audio, stop_recording
from meeting_notes.transcriber import transcribe
from meeting_notes.summarizer import generate_notes

OUTPUT_DIR = os.path.expanduser("~/meeting-notes")
DEFAULT_MODEL = os.environ.get("MEETING_NOTES_MODEL", "claude-sonnet-4-6")
DEFAULT_WHISPER_MODEL = "medium"


def copy_to_clipboard(text):
    """Copy text to macOS clipboard via pbcopy."""
    try:
        proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def slugify(text):
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


def create_meeting_dir(meeting_name):
    """Create and return the meeting output directory path."""
    today = date.today().isoformat()
    slug = slugify(meeting_name)
    dir_name = f"{today}_{slug}"
    meeting_dir = os.path.join(OUTPUT_DIR, dir_name)
    os.makedirs(meeting_dir, exist_ok=True)
    return meeting_dir, today


def cmd_start(args):
    """Handle the 'start' command: record, transcribe, summarize."""
    meeting_name = args.name
    model = args.model or DEFAULT_MODEL
    whisper_model = args.whisper_model or DEFAULT_WHISPER_MODEL

    meeting_dir, today = create_meeting_dir(meeting_name)
    audio_path = os.path.join(meeting_dir, "audio.wav")
    transcript_path = os.path.join(meeting_dir, "transcript.txt")
    notes_path = os.path.join(meeting_dir, "notes.md")

    # --- Recording ---
    print(f"Recording meeting: {meeting_name}")
    print(f"Output directory: {meeting_dir}")
    print("Press Ctrl+C to stop recording and begin processing.\n")

    process = record_audio(audio_path)

    # Wait for Ctrl+C
    def handle_sigint(sig, frame):
        pass  # break out of wait

    original_handler = signal.signal(signal.SIGINT, handle_sigint)

    try:
        process.wait()
    except Exception:
        pass

    signal.signal(signal.SIGINT, original_handler)

    print("\nStopping recording...")
    stop_recording(process)

    # Check that audio file was created and has content
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        print("Error: No audio was recorded. Check your audio device settings.", file=sys.stderr)
        print("List available devices with: ffmpeg -f avfoundation -list_devices true -i \"\"", file=sys.stderr)
        sys.exit(1)

    # --- Transcription ---
    print("\n--- Transcription ---")
    transcript = transcribe(audio_path, model_size=whisper_model)

    with open(transcript_path, "w") as f:
        f.write(transcript)
    print(f"Transcript saved: {transcript_path}")

    # --- Note Generation ---
    print("\n--- Note Generation ---")
    notes = generate_notes(transcript, meeting_name, today, model=model)

    if notes is None:
        print("Failed to generate notes. Transcript was saved.", file=sys.stderr)
        sys.exit(1)

    with open(notes_path, "w") as f:
        f.write(notes)

    print(f"\nNotes saved: {notes_path}")
    if copy_to_clipboard(notes):
        print("Notes copied to clipboard — paste into Notion with Cmd+V.")
    print("Done!")


def cmd_reprocess(args):
    """Handle the 'reprocess' command: re-run transcription and/or summarization on existing audio."""
    meeting_folder = args.folder
    model = args.model or DEFAULT_MODEL
    whisper_model = args.whisper_model or DEFAULT_WHISPER_MODEL
    notes_only = args.notes_only

    # Resolve the meeting directory
    if os.path.isabs(meeting_folder):
        meeting_dir = meeting_folder
    else:
        meeting_dir = os.path.join(OUTPUT_DIR, meeting_folder)

    if not os.path.isdir(meeting_dir):
        print(f"Error: Meeting folder not found: {meeting_dir}", file=sys.stderr)
        print("Use 'meeting-notes list' to see available meetings.", file=sys.stderr)
        sys.exit(1)

    audio_path = os.path.join(meeting_dir, "audio.wav")
    transcript_path = os.path.join(meeting_dir, "transcript.txt")
    notes_path = os.path.join(meeting_dir, "notes.md")

    # Extract meeting name and date from folder name
    folder_name = os.path.basename(meeting_dir)
    parts = folder_name.split("_", 1)
    meeting_date = parts[0] if len(parts) > 0 else "unknown"
    meeting_name = parts[1].replace("-", " ").title() if len(parts) > 1 else folder_name

    if notes_only:
        # Re-generate notes from existing transcript
        if not os.path.exists(transcript_path):
            print(f"Error: No transcript found at {transcript_path}", file=sys.stderr)
            sys.exit(1)

        with open(transcript_path, "r") as f:
            transcript = f.read()

        print(f"Reprocessing notes for: {meeting_name}")
        print("\n--- Note Generation ---")
        notes = generate_notes(transcript, meeting_name, meeting_date, model=model)
    else:
        # Re-run full pipeline from audio
        if not os.path.exists(audio_path):
            print(f"Error: No audio file found at {audio_path}", file=sys.stderr)
            sys.exit(1)

        print(f"Reprocessing: {meeting_name}")

        print("\n--- Transcription ---")
        transcript = transcribe(audio_path, model_size=whisper_model)

        with open(transcript_path, "w") as f:
            f.write(transcript)
        print(f"Transcript saved: {transcript_path}")

        print("\n--- Note Generation ---")
        notes = generate_notes(transcript, meeting_name, meeting_date, model=model)

    if notes is None:
        print("Failed to generate notes.", file=sys.stderr)
        sys.exit(1)

    with open(notes_path, "w") as f:
        f.write(notes)

    print(f"\nNotes saved: {notes_path}")
    if copy_to_clipboard(notes):
        print("Notes copied to clipboard — paste into Notion with Cmd+V.")
    print("Done!")


def cmd_list(args):
    """Handle the 'list' command: show past meetings."""
    if not os.path.isdir(OUTPUT_DIR):
        print("No meetings found.")
        return

    entries = sorted(os.listdir(OUTPUT_DIR), reverse=True)
    dirs = [e for e in entries if os.path.isdir(os.path.join(OUTPUT_DIR, e))]

    if not dirs:
        print("No meetings found.")
        return

    print("Past meetings:\n")
    for d in dirs:
        parts = d.split("_", 1)
        date_str = parts[0] if len(parts) > 0 else "unknown"
        name = parts[1].replace("-", " ").title() if len(parts) > 1 else d
        meeting_dir = os.path.join(OUTPUT_DIR, d)
        has_notes = os.path.exists(os.path.join(meeting_dir, "notes.md"))
        status = "notes ready" if has_notes else "no notes"
        print(f"  {date_str}  {name}  ({status})")


def main():
    parser = argparse.ArgumentParser(
        prog="meeting-notes",
        description="Privacy-first local meeting notes generator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start command
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
    start_parser.set_defaults(func=cmd_start)

    # reprocess command
    reprocess_parser = subparsers.add_parser(
        "reprocess", help="Re-run transcription and note generation on an existing meeting"
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
        "--notes-only",
        action="store_true",
        help="Only regenerate notes from existing transcript (skip transcription)",
    )
    reprocess_parser.set_defaults(func=cmd_reprocess)

    # list command
    list_parser = subparsers.add_parser("list", help="List past meetings")
    list_parser.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
