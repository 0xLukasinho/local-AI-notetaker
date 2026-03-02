# Meeting Notes Generator

A CLI tool that records meeting audio, transcribes it locally with Whisper, and generates structured notes using the Claude API. Audio and transcripts stay on your machine — only the transcript text is sent to the API for note generation.

```
Meeting Audio → [Whisper STT] → Transcript → [Claude API] → Structured Notes
                  (local)                      (remote)
```

## Quick Start

```bash
# Record a meeting (Ctrl+C to stop and process)
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
# Use a different Claude model
meeting-notes start "Sprint Planning" --model claude-sonnet-4-6

# Use a smaller/faster Whisper model
meeting-notes start "Quick Sync" --whisper-model small

# Use a larger/more accurate Whisper model
meeting-notes start "Board Meeting" --whisper-model large
```

Set `MEETING_NOTES_MODEL` to change the default Claude model:

```bash
export MEETING_NOTES_MODEL="claude-sonnet-4-6"
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

- **macOS** (uses AVFoundation for audio capture)
- **Python 3.9+**
- **ffmpeg** (for audio recording)
- **Anthropic API key** (for note generation via Claude)

### Step 1: Install ffmpeg

```bash
brew install ffmpeg
```

If you don't have Homebrew:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### Step 2: Install the tool

```bash
git clone <repo-url>
cd local-granola
pip3 install -e .
```

### Step 3: Set up your API key

Copy the example env file and add your Anthropic API key:

```bash
cp .env.example .env
```

Edit `.env` and replace `your-api-key-here` with your key from [console.anthropic.com](https://console.anthropic.com).

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

The first time you run the tool, macOS will prompt you to grant microphone access to Terminal. Accept this — it's required for recording.

## Audio Device Configuration

The tool defaults to the MacBook Pro Microphone (device index 1). To see available devices:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

To use a different device, update the `device_index` default in `meeting_notes/recorder.py`.

## Privacy

- Audio recording and Whisper transcription run entirely on your machine
- The transcript text is sent to the Anthropic API for note generation — this is the only network call
- All files (audio, transcript, notes) are stored locally in `~/meeting-notes/`
- Your API key is stored in `.env` which is git-ignored

## Troubleshooting

**"ffmpeg is not installed"**
Install it with `brew install ffmpeg`.

**"ANTHROPIC_API_KEY not configured"**
Copy `.env.example` to `.env` and add your API key.

**"No audio was recorded"**
Check that microphone permission is granted. Run `ffmpeg -f avfoundation -list_devices true -i ""` to see available devices.

**Processing is slow**
Use a smaller Whisper model (`--whisper-model small` or `--whisper-model base`) for faster transcription at lower accuracy.

## License

MIT
