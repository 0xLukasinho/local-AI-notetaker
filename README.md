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

Windows (the `[windows]` extra pulls in `pyaudiowpatch`, `numpy`, `keyboard`, `pycaw`, and `psutil` for capture, hotkey, and Meet microphone detection):

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

**Windows:** no extra permission grant is needed. The tool records the default microphone and system audio from every playback device (see below).

## Audio Device Configuration

**macOS** — defaults to device index 1. List available devices:

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

Update `device_index` in `meeting_notes/recorder_mac.py` to change.

**Windows** — records the default microphone, and a WASAPI loopback of **every** playback device, including ones that are not the Windows default. That covers Meet's own speaker menu, a per-app output device, and headphones that connect or disconnect after recording has already started. A loopback is tied to the endpoint it was opened on, so the recorder refreshes the device list during the call and opens or closes streams as devices appear and disappear. Losing one playback device does not stop the microphone.

The session folder gets `audio-devices.log` with the microphone, each loopback (default or not), device switches, and a warning if system audio stays silent while the microphone is active. During a watched call those lines are copied into `~/.meeting-notes/watch.log` with a `recorder:` prefix. The recorder process also prints them; the watcher saves that stream to `recorder.log` in the session folder.

## Automation

Manual use is unchanged. `meeting-notes start "Name"`, `reprocess`, `list`, the tray app, Ctrl+C, and the Ctrl+Alt+Q hotkey behave as before when you do not pass the flags below and you do not set `transcript_only` in the config file. Sessions still land in `~/meeting-notes/YYYY-MM-DD_slug`. The default path still transcribes locally and summarizes with `claude -p`.

What changed for every session, including a manual one, is additive: each folder gets a `status.json` (`recording`, `transcribing`, `done`, or `failed`, plus timestamps, pid, mode, and error), and `transcript.txt` is replaced atomically so a reader never sees a partial file. The transcript lines are still `[MM:SS] text`. A manual `start` will not overwrite an existing `transcript.txt`; pick another name or use `reprocess`.

### Transcript-only mode

```bash
meeting-notes start "Weekly Standup" --transcript-only
meeting-notes reprocess 2026-02-19_weekly-standup --no-summary
```

`--transcript-only` and `--no-summary` are the same flag. The session is recorded and transcribed, `claude` is not invoked, and `notes.md` is not written. Audio is kept, because the usual delete step only runs after notes exist. `reprocess --notes-only` still regenerates Claude notes; it cannot be combined with `--transcript-only`.

Set `transcript_only = true` in the config file to make that the default for `start` and `reprocess`. Leave it false (the default when the file is missing) to keep Claude summaries.

### Stopping a recording

Ctrl+C and the hotkey still work. A recorder with no console — the watcher starts it hidden — also stops when either of these appears:

- `STOP` inside the session folder
- `~/.meeting-notes/stop-request.json` naming that session's pid or folder

```bash
meeting-notes stop
```

`stop` writes both of those and, when the recorder was started as its own Windows process group, also sends `CTRL_BREAK_EVENT`. The recorder flushes the WAV, then transcribes. It does not need a console attached.

### Google Meet watcher

```bash
meeting-notes watch
```

On Windows this loops until Ctrl+C. A call **starts** only when both are true:

1. A top-level Chrome window is titled like `Meet - <code or title>` (the ` - Google Chrome` suffix is ignored). Chrome puts only the **active tab** in the window title, so a Meet in a background tab is invisible until that tab is focused.
2. `chrome.exe` is using the microphone. Two probes are OR-ed, and either one is enough:
   - **WASAPI** capture sessions on active input devices (`pycaw` + `psutil`). A session counts when its state is Active and the process name contains `mic_process_marker`.
   - The legacy consent store `HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged`, where `LastUsedTimeStop == 0` means the mic is in use. Windows 11 26H2 (and some earlier builds) stop updating this key, so it is not sufficient on its own. It is still checked so older Windows keeps working.

If `pycaw` is not installed, the watcher logs that once and uses the registry probe only. It is part of the `[windows]` extra. On an existing install, add it with `pip install pycaw` (`pycaw` 20260926 and newer need Python 3.10+; Python 3.9 should install `pycaw>=20251023,<20260926`).

The Meet lobby title does not match `Meet - <code or title>`, so a preview microphone on the pre-join screen does not start a recording. The Meet tab has to be focused while the microphone is in use at least once. After that coincident sample, either signal keeps the join timer running, so muting or switching to another Chrome tab during `join_debounce_seconds` (default 12) does not cancel it. The session is named from the Meet code when there is one, otherwise the window title.

**After the call has started**, mute and tab switches are handled separately. Muting in Meet can release the capture session, and a muted stretch is often longer than the leave debounce, so the watcher keeps the session while that Meet window is still open. Switching to another Chrome tab hides the Meet title; the session stays while the microphone capture session is still active. The session stops only after **both** have been gone for `leave_debounce_seconds` (default 25).

That choice has a limit. A Meet tab left open on the post-call page looks the same as a mute (the title is still a Meet title, the mic is already released), so recording continues until you switch away from that tab or close it. A mute that releases the mic **and** a Meet tab that is not the active tab for longer than the leave debounce also looks like leaving, and the session ends. Those two cases cannot be told apart without reading Chrome's background tabs.

The log (`~/.meeting-notes/watch.log`) records an INFO line whenever the microphone signal or the Meet-window signal changes, including which microphone probe said yes (`registry=yes/no/unavailable`, `wasapi=yes/no/unavailable`). A probe that cannot run is logged once with the reason.

If a session is already recording, the watcher does not start a second one and logs that. A manual `meeting-notes start` while the watcher is recording prints a warning and does not start either. Stop the current session with `meeting-notes stop` first.

Watcher sessions use a unique folder. The first one that day is still `YYYY-MM-DD_slug`. If that folder exists, the next is `YYYY-MM-DD_slug_HHMM`, then `YYYY-MM-DD_slug_HHMM_2`, and so on. Manual `start` keeps the unsuffixed name.

Logs rotate at `~/.meeting-notes/watch.log`.

`joined_at` is when the join debounce confirms and recording starts. `left_at` is when recording stops (the leave debounce has already elapsed). `duration_seconds` is that interval.

### Delivery

When a watcher session finishes, the watcher POSTs JSON:

`folder_name`, `folder_path`, `meet_code`, `window_title`, `joined_at`, `left_at`, `duration_seconds`, `whisper_model`, `language`, `transcript`, `chunk_index`, `chunk_count`, `transcript_sha256`, `transcript_chars`.

`transcript` in each request is one piece of the file. `chunk_index` starts at 0. `chunk_count` is how many requests make up this delivery. `transcript_sha256` (SHA-256 of the UTF-8 file) and `transcript_chars` describe the **full** transcript and are the same on every piece. Concatenate `transcript` in `chunk_index` order. A short transcript is a single request (`chunk_count` 1) whose `transcript` is the whole file.

Pieces stay under `webhook_max_body_bytes` (default 6000). One JSON body around 8 KB was arriving cut off mid-sentence — about 7 to 9 minutes into the call — even though `transcript.txt` on disk was complete. Set `webhook_max_body_bytes` to `0` to send a single request again.

The request uses `webhook_auth_header` / `webhook_auth_value` when both are set, on every piece. Put the full header value in config (`Bearer <key>` when the header name is `Authorization`). Failures retry with exponential backoff. If they still fail, the payload (the full transcript, not the secret) is written to `~/.meeting-notes/outbox/` and retried when the watcher starts and every `outbox_retry_seconds`. A retry sends every piece again, so a receiver should replace on `(folder_name, chunk_index)` rather than append a duplicate. A successful delivery sets `delivered` and `delivered_at` in `status.json`. `delete_audio_after_delivery` (default false) removes `audio.wav` only after that success. The watcher log records the character count and how many requests were posted. The auth value is never written to the log, the outbox, or `status.json`.

```bash
meeting-notes deliver 2026-10-29_abc-defg-hij_1430
```

`deliver` resends any session that already has a `transcript.txt`.

### Config

Copy `config.example.toml` to `~/.meeting-notes/config.toml`, or use `config.json` with the same keys. `config.toml` wins when both exist. `MEETING_NOTES_CONFIG` points at a different file. Environment variables override the webhook URL and auth header.

| Key | Default | Role |
| --- | --- | --- |
| `webhook_url` | empty | POST target. Empty skips delivery and does not fill the outbox. |
| `webhook_auth_header` | `Authorization` | Header name. Omitted when the value is empty. |
| `webhook_auth_value` | empty | Header value, sent as-is (`Bearer <key>` for an Authorization bearer token). Never written to the session, the log, or the outbox. |
| `webhook_max_body_bytes` | `6000` | Max JSON body per POST. Longer transcripts are several requests. `0` sends one request. |
| `join_debounce_seconds` | `12` | Hold time before the watcher starts recording. |
| `leave_debounce_seconds` | `25` | Hold time before the watcher stops recording. |
| `poll_interval_seconds` | `2` | How often window and microphone state are checked. |
| `outbox_retry_seconds` | `60` | How often failed deliveries are retried. |
| `transcript_only` | `false` | Default for manual `start` / `reprocess`. |
| `watcher_transcript_only` | `true` | Watcher sessions skip Claude notes. |
| `delete_audio_after_delivery` | `false` | Remove `audio.wav` after a successful POST. |
| `window_title_regex` | empty | Replaces the built-in `Meet - ...` pattern. |
| `mic_process_marker` | `chrome.exe` | Substring matched against the process using the microphone (registry subkey and WASAPI capture session). |
| `whisper_model` | `medium` | Whisper size for watcher sessions. |
| `language` | `en` | Language forced for watcher sessions. |

### Autostart

Windows only. Registers the watcher at login with `pythonw` (no console window), via Task Scheduler, or a Startup-folder script if `schtasks` fails.

```bash
meeting-notes watch --install
meeting-notes watch --status
meeting-notes watch --uninstall
```

`--status` prints whether login autostart is registered, whether a watcher process is running, and where the config and log are.

## Privacy

- Audio recording and Whisper transcription run entirely on your machine
- The transcript text is sent to Claude via the Claude Code CLI for note generation — this is the only network call on the default `start` / `reprocess` path
- Transcript-only mode and the Meet watcher do not call Claude. If `webhook_url` is set, the watcher POSTs the transcript to that URL after the session finishes
- All files (audio, transcript, notes) are stored locally in `~/meeting-notes/`
- No API key is stored; authentication is whatever your Claude Code login is. Webhook credentials live only in `~/.meeting-notes/config.toml` or the `MEETING_NOTES_WEBHOOK_*` environment variables

## Troubleshooting

**"ffmpeg is not installed"**
Install it with `brew install ffmpeg`.

**"Claude Code CLI ('claude') not found on PATH"**
Install Claude Code from [claude.com/code](https://claude.com/code), then run `claude login`. Reopen your terminal so the PATH refresh takes effect.

**"No audio was recorded"**
On macOS, check that microphone permission is granted and run `ffmpeg -f avfoundation -list_devices true -i ""` to see available devices.
On Windows, check that a playback device and a default microphone are enabled in *Settings → System → Sound*. The session's `audio-devices.log` lists the devices the recorder actually opened.

**Transcript contains only your own voice**
System audio is a WASAPI loopback per playback device. The recorder follows devices that appear, disappear, or become the default during the call, and it also records a device that is not the default (so Meet can play on headphones while speakers stay the Windows default). `audio-devices.log` and `watch.log` (`recorder:` lines) should show the headphones or speakers you were listening through, and a `default output switched` line when that changes. `system audio silent for 60s while the microphone is active` means those loopbacks heard nothing while you were speaking; the recorder reopens them and logs the open devices. Restart the watcher after updating so the next call uses this path.

**Webhook transcript stops after a few minutes**
`transcript.txt` is the full text. The webhook is several JSON bodies when that file is longer than `webhook_max_body_bytes`. Join `transcript` by `chunk_index`. `watch.log` lines `Posting transcript (N chars, M request(s), max body B bytes)` are the size that was sent. The `Authorization` header is unchanged and is not logged.

**Watcher never notices a Google Meet**
Open `~/.meeting-notes/watch.log`. `Microphone not in use (registry=no, wasapi=unavailable)` means the WASAPI probe is off (usually `pycaw` is not installed: `pip install pycaw`) and the consent-store registry also did not show Chrome. `Meet window not detected` while you are in the call means the Meet tab is not the active Chrome tab; focus it for the join hold (`join_debounce_seconds`, default 12). After the session has started, a mute or a background tab does not by itself end it.

**"global hotkey unavailable" on Windows**
The `keyboard` library couldn't install its low-level hook (often due to AV software). The tool falls back to Ctrl+C in the terminal. To use the hotkey, try running the terminal as administrator.

**Processing is slow**
Use a smaller Whisper model (`--whisper-model small` or `--whisper-model base`) for faster transcription at lower accuracy.

## License

MIT
