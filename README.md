# Meeting Notes Generator

A CLI tool that records meeting audio, transcribes it locally with Whisper, and generates structured notes by invoking the Claude Code CLI (which uses your Claude subscription — no API key). Audio and transcripts stay on your machine; only the transcript text is sent to Claude for note generation.

```
Meeting Audio → [Whisper STT] → Transcript → [claude -p] → Structured Notes
                  (local)                     (subscription)
```

## Quick Start

```bash
# Record a meeting (Ctrl+Alt+Q on Windows, Cmd+Alt+Q on macOS to stop; Ctrl+C also works)
meeting-notes start "Weekly Standup"

# Re-generate notes from an existing transcript
meeting-notes reprocess 2026-02-19_weekly-standup --notes-only

# Re-run transcription + notes from existing audio
meeting-notes reprocess 2026-02-19_weekly-standup

# List past meetings
meeting-notes list
```

When processing completes, notes are automatically copied to your clipboard — paste directly into Notion with Cmd+V.

## Options

```bash
# Use a different Claude model (default: claude-opus-4-7)
meeting-notes start "Sprint Planning" --model claude-sonnet-4-6

# Use a smaller/faster Whisper model
meeting-notes start "Quick Sync" --whisper-model small

# Use a larger/more accurate Whisper model
meeting-notes start "Board Meeting" --whisper-model large
```

Set `MEETING_NOTES_MODEL` to change the default Claude model:

```bash
export MEETING_NOTES_MODEL="claude-opus-4-7"
```

## Output

Each meeting creates a folder in `~/meeting-notes/`:

```
~/meeting-notes/
  2026-02-19_weekly-standup/
    audio.wav         # Raw audio recording
    transcript.txt    # Timestamped transcript
    notes.md          # Structured notes (auto-copied to clipboard)
```

## Installation

### Prerequisites

- **macOS** (uses AVFoundation) or **Windows 10/11** (uses WASAPI loopback)
- **Python 3.9+**
- **ffmpeg** (required by Whisper for audio decoding; also handles capture on macOS)
- **Claude Code CLI** (`claude` on PATH, logged in via `claude login`) — used to generate notes against your Claude subscription

### Step 1: Install ffmpeg

macOS:

```bash
brew install ffmpeg
```

Windows:

```powershell
winget install ffmpeg
# or: choco install ffmpeg
```

### Step 2: Install the tool

macOS:

```bash
git clone <repo-url>
cd local-AI-notetaker
pip3 install -e .
```

Windows (the `[windows]` extra pulls in `pyaudiowpatch`, `numpy`, and `keyboard` for capture + hotkey):

```powershell
git clone <repo-url>
cd local-AI-notetaker
pip install -e ".[windows]"
```

### Step 3: Set up Claude Code

Install Claude Code from [claude.com/code](https://claude.com/code) and log in:

```bash
claude login
```

That's it — no API key needed. Note generation runs against whichever subscription that login is authenticated to, with no per-meeting cost.

### Step 4: Verify your PATH

If `meeting-notes` is not found after installation, add the Python scripts directory to your PATH:

```bash
echo 'export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify it works:

```bash
meeting-notes --help
```

### Step 5: Grant microphone access

**macOS:** the first time you run the tool, macOS will prompt you to grant microphone access to Terminal. Accept this — it's required for recording.

**Windows:** no extra permission grant is needed. The tool uses the default speaker (for loopback / system audio) and default microphone configured in *Settings → System → Sound*.

## Audio Device Configuration

**macOS** — defaults to device index 1. List available devices:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

Update `device_index` in `meeting_notes/recorder_mac.py` to change.

**Windows** — uses whatever you've set as the default playback and recording devices in Windows Sound settings. To change which apps the tool captures, change your Windows defaults.

## Privacy

- Audio recording and Whisper transcription run entirely on your machine
- The transcript text is sent to Claude via the Claude Code CLI for note generation — this is the only network call
- All files (audio, transcript, notes) are stored locally in `~/meeting-notes/`
- No API key is stored; authentication is whatever your Claude Code login is

## Troubleshooting

**"ffmpeg is not installed"**
Install it with `brew install ffmpeg`.

**"Claude Code CLI ('claude') not found on PATH"**
Install Claude Code from [claude.com/code](https://claude.com/code), then run `claude login`. Reopen your terminal so the PATH refresh takes effect.

**"No audio was recorded"**
On macOS, check that microphone permission is granted and run `ffmpeg -f avfoundation -list_devices true -i ""` to see available devices.
On Windows, check that you have a default speaker and microphone set in *Settings → System → Sound*.

**"global hotkey unavailable" on Windows**
The `keyboard` library couldn't install its low-level hook (often due to AV software). The tool falls back to Ctrl+C in the terminal. To use the hotkey, try running the terminal as administrator.

**Processing is slow**
Use a smaller Whisper model (`--whisper-model small` or `--whisper-model base`) for faster transcription at lower accuracy.

## License

MIT
