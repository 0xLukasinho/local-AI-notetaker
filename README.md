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
    audio.wav         # Mixed microphone + system audio
    audio-self.wav    # Microphone only (Windows, speaker labels on)
    audio-others.wav  # Other participants only (Windows, speaker labels on)
    speakers.jsonl    # Meet names captured during the call, when the extension is loaded
    transcript.txt    # [MM:SS] Name: text
    transcript.json   # segments: start, end, speaker, text
    notes.md          # Structured notes (auto-copied to clipboard)
```

`audio-self.wav`, `audio-others.wav`, `speakers.jsonl`, and `transcript.json` are written when speaker labels are on. Set `speaker_labels = false` to keep a single `audio.wav` and unlabeled `[MM:SS] text` lines. See [Speaker names](#speaker-names).

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

What changed for every session, including a manual one, is additive: each folder gets a `status.json` (`recording`, `transcribing`, `done`, or `failed`, plus timestamps, pid, mode, and error), and `transcript.txt` is replaced atomically so a reader never sees a partial file. With the default `speaker_labels = true` on Windows, lines are `[MM:SS] Name: text`. `speaker_labels = false` keeps `[MM:SS] text`. A manual `start` will not overwrite an existing `transcript.txt`; pick another name or use `reprocess`.

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

### Google Meet and Slack Huddle watcher

```bash
meeting-notes watch
```

On Windows this loops until Ctrl+C. It records **one call at a time** and, when the call ends, delivers that session the same way a Meet watch session already does. `meeting-notes start` is not part of that path: it does not watch for calls, and it does not POST the webhook.

Google Meet and Slack Huddle can both be turned off in config (`watch_google_meet`, `watch_slack_huddle`). When a config file omits them, both stay on.

#### Google Meet

A Meet **starts** only when both are true:

1. A top-level Chrome window is titled like `Meet - <code or title>` (the ` - Google Chrome` suffix is ignored). Chrome puts only the **active tab** in the window title, so a Meet in a background tab is invisible until that tab is focused.
2. `chrome.exe` is using the microphone. Two probes are OR-ed, and either one is enough:
   - **WASAPI** capture sessions on active input devices (`pycaw` + `psutil`). A session counts when its state is Active and the process name contains `mic_process_marker`.
   - The legacy consent store `HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged`, where `LastUsedTimeStop == 0` means the mic is in use. Windows 11 26H2 (and some earlier builds) stop updating this key, so it is not sufficient on its own. It is still checked so older Windows keeps working.

If `pycaw` is not installed, the watcher logs that once and uses the registry probe only. It is part of the `[windows]` extra. On an existing install, add it with `pip install pycaw` (`pycaw` 20260926 and newer need Python 3.10+; Python 3.9 should install `pycaw>=20251023,<20260926`).

The Meet lobby title does not match `Meet - <code or title>`, so a preview microphone on the pre-join screen does not start a recording. The Meet tab has to be focused while the microphone is in use at least once. After that coincident sample, either signal keeps the join timer running, so muting or switching to another Chrome tab during `join_debounce_seconds` (default 12) does not cancel it. The session is named from the Meet code when there is one, otherwise the window title.

**After the call has started**, mute and tab switches are handled separately. Muting in Meet can release the capture session, and a muted stretch is often longer than the leave debounce, so the watcher keeps the session while that Meet window is still open. Switching to another Chrome tab hides the Meet title; the session stays while the microphone capture session is still active. The session stops only after **both** have been gone for `leave_debounce_seconds` (default 25).

That choice has a limit. A Meet tab left open on the post-call page looks the same as a mute (the title is still a Meet title, the mic is already released), so recording continues until you switch away from that tab or close it. A mute that releases the mic **and** a Meet tab that is not the active tab for longer than the leave debounce also looks like leaving, and the session ends. Those two cases cannot be told apart without reading Chrome's background tabs.

The log (`~/.meeting-notes/watch.log`) records an INFO line whenever the microphone signal or the Meet-window signal changes, including which microphone probe said yes (`registry=yes/no/unavailable`, `wasapi=yes/no/unavailable`). A probe that cannot run is logged once with the reason.

If a session is already recording — a Meet, a Huddle, or a manual `meeting-notes start` — the watcher does not start a second one and logs that. A manual `meeting-notes start` while the watcher is recording prints a warning and does not start either. Stop the current session with `meeting-notes stop` first.

If a Meet is already detected and `slack.exe` is also on the microphone before any recording has started, the Meet is the one that is recorded. The Huddle is logged and ignored until the Meet session has ended (its own leave rule, then the leave debounce). The reverse is the same: a Huddle that is already recording keeps going when a Meet starts, and the Meet is not recorded until the Huddle has ended. A Meet tab left open on the call or the post-call page still counts as the Meet, so a Huddle will not start until you switch away from that tab or close it. An open Meet tab with Chrome's microphone already released does not block a desktop Huddle.

Watcher sessions use a unique folder. The first one that day is still `YYYY-MM-DD_slug`. If that folder exists, the next is `YYYY-MM-DD_slug_HHMM`, then `YYYY-MM-DD_slug_HHMM_2`, and so on. Manual `start` keeps the unsuffixed name.

The watcher rewrites `~/.meeting-notes/heartbeat.json` about every 30 seconds with the time, pid, version, and state (`idle` or `recording`). State stays `recording` through transcription, so a long transcript is not treated as a hang. A watcher session is `recording` whenever its recorder is still running, including when that recorder is a child process: the heartbeat and `watch --status` then include the session folder and the recorder pid. `--ensure` uses the same check, so a heartbeat file that still says `idle` does not restart the watcher in the middle of that session. `~/.meeting-notes/watch.log` records why the watcher started, why it stopped, any unhandled exception (with the traceback), and any hang-kill or restart. The log rotates at 1 MB and keeps 3 older files.

```bash
meeting-notes watch --ensure
meeting-notes watch --status
```

`--ensure` exits immediately when a healthy watcher is already running. When none is running, or the heartbeat is older than 3 minutes, it starts one hidden `pythonw` process. A heartbeat that says `recording`, and a live watcher session whose heartbeat still says `idle`, are left alone until the heartbeat is older than 10 minutes. A pid that does not hold the watcher lock is never killed. Two watchers cannot run at once: the live process holds a lock that disappears if that process crashes. If the start fails, or the new process exits straight away, `--ensure` tries again after 30 seconds, then 60, then 120, and writes each attempt to `watch.log`. That covers a logon where the audio devices or the network are not ready yet.

`joined_at` is when the join debounce confirms and recording starts. `left_at` is when recording stops (the leave debounce has already elapsed). `duration_seconds` is that interval.

#### Slack Huddle

A desktop Huddle is `slack.exe` holding the microphone. The same two probes as Meet are OR-ed (the consent-store registry and WASAPI capture sessions). The window title is not part of the decision. Slack's own Huddle window, including a popped-out one, does not put "Huddle" in the title.

It **starts** after that microphone signal has held for `join_debounce_seconds` (default 12). A voice clip or a Preferences mic test shorter than that does not start a recording. It **ends** after `slack.exe` has released the microphone for `leave_debounce_seconds` (default 25). A Huddle window left open does not keep the recording going.

The session name comes from a `slack.exe` window title when one clearly has a channel or DM: a `#channel`, a `(Channel)` / `(DM)` / `(Group DM)` label, or Slack's usual `conversation - workspace - Slack` title. `Activity`, `Preferences`, `Slack` alone, and `Workspace - Slack` are not used. Otherwise the name is `Slack Huddle` plus the time the join is confirmed, for example `Slack Huddle 15:04`. Switching Slack views while the mic stays held does not start a second session. `status.json` and the webhook body include `source` `slack-huddle` (Meet watch sessions use `google-meet`).

**Slack in the browser is not detected.** `chrome.exe` has one microphone for every tab, and a Slack tab left open does not mean a Huddle is in progress. That cannot be told apart from a voice clip, another site using the mic, or a Meet whose tab is in the background. Meet keeps `chrome.exe`. Use the Slack desktop app for automatic Huddle recording.

**Limitations.**

- A mute that makes `slack.exe` release the microphone ends the session after the leave debounce. Leaving the Huddle does the same thing. If Slack keeps the capture session while you are muted, the recording continues until the mic is actually released.
- A voice clip or mic test that holds the microphone longer than the join debounce (default 12 seconds) starts a recording. Shorter ones do not.
- Huddles joined from Slack in Chrome, Edge, or Firefox are not recorded.
- The channel or DM is only as good as the window title. When the title is just `Slack`, the session is `Slack Huddle` plus the time.
- Participant names are not read from Slack. The transcript is your microphone against everyone else (`speaker_labels`). The Meet Chrome extension is not started for a Huddle. A future source could supply names the way that extension does; this watcher does not.
- One recording at a time, shared with Meet and with manual `meeting-notes start`.

### Speaker names

On Windows the recorder writes three files that share one clock: `audio.wav` (the mix, unchanged), `audio-self.wav` (microphone), and `audio-others.wav` (every WASAPI loopback, summed). Whisper runs on the microphone and on the loopback separately. Microphone lines use `self_name` (default `You`). Loopback lines are other people.

Real names come from Google Meet. A small unpacked Chrome extension reads the participant list, the active-speaker ring, and live captions (only if you turn captions on) from the Meet tab and POSTs them to `127.0.0.1` only while a recording is running. Nothing is injected into the page, so other participants cannot see it. There is no meeting bot and no Meet API.

Remote lines are named from Meet live captions. Turn captions on with the CC button; they are shown only to you. Each remote stretch is labeled with the caption speaker on screen at that time. If no caption covers it, a tile that shows exactly one speaker is used. If several people are highlighted at once, the line is `Remote` rather than `Daniel Bronheim / Mahmoud Lababidi`. A two-person call has one other person, so that name is used when captions are off.

People talking over each other stay whole sentences. A turn is built for each person on each track before the tracks are merged, so a short "yeah" is its own line and does not chop the other sentence into single words.

While a call is recording, `speakers.jsonl` gets a `caption_speaker` field on every sample (empty when captions are off). If captions stay off, the watcher log says to press CC. The same note is printed when the transcript is written.

```bash
meeting-notes speakers
```

That copies the extension to `~/.meeting-notes/chrome-extension` with `speaker_bridge_port` (default 8765) baked in, and prints the steps. In Chrome, once:

1. Open `chrome://extensions`
2. Turn on Developer mode
3. Load unpacked and choose that folder

Reload the extension if you change the port and run `meeting-notes speakers` again. `meeting-notes speakers --path` prints the folder only.

Without the extension, or when Meet's page structure no longer matches, recording still works. Your lines stay `self_name`. Everyone else is `Them`. If Meet exposes a stable anonymous id but no display name, those become `Speaker A`, `Speaker B`, and so on. The default `self_name` of `You` is replaced by your Meet display name when the extension reports it. Set `self_name` to a real name to keep that label instead.

Captions are what names people on a call with more than one guest. Leave them off and naming falls back to a tile that shows exactly one speaker, then to `Remote` when several people are highlighted at once. Turn captions on with CC (only you see them). If Meet changes its page and the reader no longer finds caption rows, samples simply omit them, `caption_speaker` stays empty, and the recording continues.

#### Update on the PC

No new packages. From the repo:

1. `git pull`
2. `meeting-notes speakers` (refreshes the extension copy), then in `chrome://extensions` click Reload on Meeting Notes Speaker Tags
3. In Meet, press CC so live captions are on. Other people do not see them
4. End the running watcher and start it again so it loads this code. If it is the hidden autostart process, end that `pythonw` in Task Manager and run `meeting-notes watch --ensure`. A watcher that is already healthy keeps the code it started with.

On the next call with two guests, each remote sentence should carry that person's name. `speakers.jsonl` shows `caption_speaker` on each sample. If you forget CC, `watch.log` and the transcript step say to press it, and unnamed remote lines say `Remote`.

Voice diarization stays off (`diarize = false`). Leave it off. Turning it on needs extra packages and is not part of this setup.

`transcript.txt` lines look like:

```
[00:12] Alex Chen: I can take the action item
[00:18] Ada Lovelace: I will send the deck
```

`transcript.json` has `self_name`, `participants`, `speaker_source` (`meet`, `tracks`, `diarized`, or `mix`), `captions` (`on` or `off` when the extension reported in), `limits`, and `segments` (`start`, `end`, `speaker`, `text`, `track`). The webhook keeps every existing field and adds `self_name`, `participants`, `speaker_source`, and `speakers` (`start`, `end`, `speaker`, `text`). `speakers` is split across the same chunks as the transcript lines. `participants` and `self_name` are repeated on every chunk.

Limits, also stored on `transcript.json`:

- Other people in the room with you are on the microphone, so they get `self_name`.
- Remote audio is one mix. A stretch is named from the caption speaker at that time. With captions off, one active tile is used. Otherwise the line is `Remote`. Distinct people are not merged into one combined label.
- A two-person call has one remote person. That name is used when no caption covers the stretch.
- A conference-room speakerphone is a single Meet name.
- Names are only as good as the Meet tab the extension can see. Captions can be off. A background Meet tab, or a Meet UI change, falls back to `Remote`, `Them`, or `Speaker A` / `Speaker B`.
- Loudspeakers can leak remote voices into the microphone. When the wording matches, the named remote line is kept. Headphones avoid that.

macOS still records one device. Speaker labels there are applied only when `speakers.jsonl` exists; otherwise the transcript stays unlabeled. The watcher itself is Windows-only.

`meeting-notes start`, `stop`, Ctrl+C, Ctrl+Alt+Q, and the tray app are unchanged. `meeting-notes watch` records Slack Huddles as well as Google Meet. The watcher does not require the extension.

### Delivery

When a watcher session finishes, the watcher POSTs JSON:

`folder_name`, `folder_path`, `meet_code`, `window_title`, `joined_at`, `left_at`, `duration_seconds`, `whisper_model`, `language`, `transcript`, `chunk_index`, `chunk_count`, `transcript_sha256`, `transcript_chars`. Watcher sessions also send `source` (`google-meet` or `slack-huddle`) and `meeting_name` (the Meet code, the Huddle channel or DM, or `Slack Huddle` plus the time). Manual sessions and older watcher sessions omit those two.

When the session has `transcript.json`, each body also has `self_name`, `participants`, and `speaker_source`. `speakers` is the segment list for the lines in that body's `transcript` (same order). Older sessions without `transcript.json` omit those fields, so an existing consumer keeps working.

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
| `watch_google_meet` | `true` | Watcher records Google Meet. `false` ignores Meet. |
| `watch_slack_huddle` | `true` | Watcher records Slack Huddles. `false` ignores Huddles. |
| `mic_process_marker` | `chrome.exe` | Substring matched against the process using the microphone for Google Meet (registry subkey and WASAPI capture session). |
| `whisper_model` | `medium` | Whisper size for watcher sessions. |
| `language` | `en` | Language forced for watcher sessions. |
| `self_name` | `You` | Label for the microphone. `You` is replaced by the Meet display name when the extension reports one. |
| `speaker_labels` | `true` | `false` keeps unlabeled `[MM:SS] text` and skips the separate tracks. |
| `speaker_bridge_port` | `8765` | Loopback port for the optional Meet extension. |
| `diarize` | `false` | Leave off. Captions name remote speakers. `true` is an optional local voice split and needs extra packages. |
| `diarize_backend` | `auto` | `auto`, `builtin`, `speechbrain`, `resemblyzer`, `pyannote`, or `off`. |
| `diarize_max_speakers` | `8` | Hint for pyannote. Distant voices are not merged to fit it. |
| `hf_token` | empty | Optional Hugging Face token for pyannote. Prefer `HF_TOKEN`. Never commit it. |
| `watcher_health_ping` | `false` | When true, and `webhook_url` is set, POST one `{"type": "watcher-health"}` event if the watcher starts after being down more than 15 minutes on the same local day. Not a transcript. |

### Autostart

Windows only. One per-user scheduled task (no admin) runs `meeting-notes watch --ensure` hidden, without a console window and without taking focus:

- at system startup, about 30 seconds after boot, when Windows allows that without an administrator
- at logon, about 15 seconds after login, so the profile is ready
- when the session is unlocked
- on resume from sleep, when Windows accepts that trigger

There is no repeating check. A normal PowerShell can register logon, unlock, and resume. The system-startup trigger is added only from an Administrator PowerShell; otherwise install skips startup and says so. `--ensure` starts a watcher only when one is not already healthy, so unlock does not open a second copy. If the start fails, or the new process exits straight away, the same run tries again after 30 seconds, then 60, then 120. Each attempt is logged. The task replaces a Startup-folder `MeetingNotesWatch.vbs`. Install deletes that shortcut when it is present and does not create a new one.

From the repo:

```bash
git pull
pip install -e ".[windows]"
meeting-notes autostart install
meeting-notes autostart status
meeting-notes watch --status
```

`autostart install` registers the task and starts a watcher immediately when one is not already running. Run it from a normal PowerShell. Logon, unlock, and resume do not need an administrator. System startup does: if this window is not elevated, startup is skipped and the command says so. Open PowerShell with **Run as administrator** and run `meeting-notes autostart install` again to add the startup trigger.

`autostart status` and `watch --status` print the same picture: whether the task is registered, whether the process is running, and the heartbeat age and state. While a watcher session is recording, the heartbeat line includes that session's folder and pid. `meeting-notes watch --install` is the same as `autostart install`. `meeting-notes autostart uninstall` removes the task and any leftover Startup shortcut.

In Task Scheduler the task is named `MeetingNotesWatch`. It is hidden; turn on **View → Show hidden tasks** to see it there. You do not need to. `autostart status` is the check.

`watch.log` is the place a failure shows up: `ensure attempt 1/4`, `retrying in 30s`, `ensure: starting watcher`, `hang-kill`, `ensure: restarting watcher`, `Unhandled exception`, and `Watcher stopped` all have a timestamp. A logon where the watcher is already running is one line, `ensure attempt 1/4 succeeded`.

Optional: set `watcher_health_ping = true` in `~/.meeting-notes/config.toml` when `webhook_url` is already set. If the watcher then starts after a gap longer than 15 minutes, and that gap begins and ends on the same local calendar day, it POSTs one small JSON body:

`type` (`watcher-health`), `down_seconds`, `previous_state`, `previous_heartbeat_at`, `pid`, `version`, `started_at`.

There is no transcript in that body, it does not go through the transcript outbox, and a receiver can ignore `type: watcher-health`. The webhook auth header is the same one used for transcripts. The auth value is not in the body and is not written to the log. The ping stays off until you set the key.

Limits:

- Install needs `schtasks` and a normal user account. If registration fails, the command prints the `schtasks` error, appends it to `watch.log`, and leaves autostart uninstalled. It does not write a Startup-folder shortcut.
- System startup is registered from an Administrator PowerShell. From a normal PowerShell, install skips startup and says so. Logon is what runs `--ensure` after you sign in. If every `schtasks` attempt is denied, the error tells you to rerun the command from an Administrator PowerShell.
- If the resume-from-sleep trigger is rejected, install keeps logon and unlock and says so.
- A watcher that is actually recording (including while Whisper runs, and including a recorder child whose heartbeat file still says `idle`) is not killed for a stale heartbeat until that heartbeat is older than 10 minutes. A stuck recording can keep going until then. Killing it also stops its recorder.
- A pid left in the lock file is not killed unless that process still holds the watcher lock, so a reused pid is not a random program that gets stopped.
- The health ping cannot tell a crash from a laptop that slept for more than 15 minutes on the same day. Overnight (the previous heartbeat was on an earlier local date) does not ping. A gap that crosses local midnight does not ping. There is no timer during the day; the next `--ensure` is at unlock, resume, or the next logon.
- `git pull` does not reload a watcher that is already running. After an update, end the hidden `pythonw` process (Task Manager) and run `meeting-notes watch --ensure`.
- The replacement watcher is started with WMI (`Win32_Process.Create`) so it keeps running after the scheduled task exits. The command uses the real `pythonw.exe` (`sys.executable`, or `sys._base_executable` and the `pythonw.exe` beside it when the Microsoft Store path is an app-execution alias). That alias cannot be launched by `Win32_Process.Create`. If WMI still fails, `--ensure` starts a detached process instead and logs one INFO line, `using popen fallback`, without the PowerShell error record. A detached process can be stopped when the task exits. The same `--ensure` run retries after 30, 60, and 120 seconds when the start does not stick. The task's time limit is 10 minutes, which covers those waits.
- The watcher and autostart are Windows-only.

## Privacy

- Audio recording and Whisper transcription run entirely on your machine
- The transcript text is sent to Claude via the Claude Code CLI for note generation — this is the only network call on the default `start` / `reprocess` path
- Transcript-only mode and the watcher do not call Claude. If `webhook_url` is set, the watcher POSTs the transcript to that URL after a Meet or a Huddle session finishes. A manual `meeting-notes start` does not POST it. With `watcher_health_ping = true` (default false), a long same-day outage also POSTs one `watcher-health` event to that same URL. That event is not a transcript
- The optional Chrome extension sends participant names, the active-speaker highlight, and caption lines (only when you have captions on) to `127.0.0.1` on this computer only. It does not join the call or change what other people see
- Naming remote speakers uses those local caption lines. No extra model is downloaded for that
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

**Action items are attributed to the wrong person**
Open the session's `transcript.json`. `captions` is `off` when CC was not on: remote lines say `Remote` or a single tile name, and `watch.log` tells you to press CC. `tracks` means the extension never reached the bridge: remote lines say `Them`. Confirm `meeting-notes speakers` was reloaded in Chrome, the Meet tab was open, and `speakers.jsonl` has `caption_speaker` filled in during the call. `meet` means a caption or a single tile supplied a real name. A line is not labeled `Ada / Grace`. Two people talking at once should be two lines. Someone sitting next to you is on the microphone track.

**Transcript contains only your own voice**
System audio is a WASAPI loopback per playback device. The recorder follows devices that appear, disappear, or become the default during the call, and it also records a device that is not the default (so Meet can play on headphones while speakers stay the Windows default). `audio-devices.log` and `watch.log` (`recorder:` lines) should show the headphones or speakers you were listening through, and a `default output switched` line when that changes. `system audio silent for 60s while the microphone is active` means those loopbacks heard nothing while you were speaking; the recorder reopens them and logs the open devices. Restart the watcher after updating so the next call uses this path.

**Webhook transcript stops after a few minutes**
`transcript.txt` is the full text. The webhook is several JSON bodies when that file is longer than `webhook_max_body_bytes`. Join `transcript` by `chunk_index`. `watch.log` lines `Posting transcript (N chars, M request(s), max body B bytes)` are the size that was sent. The `Authorization` header is unchanged and is not logged.

**The watcher was not running and a call was missed**
Run `meeting-notes watch --status`. `Watcher process: not running` or a heartbeat older than a few minutes means it was down. `~/.meeting-notes/watch.log` has the start, stop, hang-kill, startup retries (`retrying in 30s`, `60s`, `120s`), and any traceback. `meeting-notes autostart status` should say the scheduled task is installed (startup or logon, unlock, and resume when Windows allowed them). If it says not installed, run `meeting-notes autostart install`. The task runs `--ensure` at startup or logon, on unlock, and on resume. It does not poll during the day.

**Watcher never notices a Google Meet**
Open `~/.meeting-notes/watch.log`. `Microphone not in use (registry=no, wasapi=unavailable)` means the WASAPI probe is off (usually `pycaw` is not installed: `pip install pycaw`) and the consent-store registry also did not show Chrome. `Meet window not detected` while you are in the call means the Meet tab is not the active Chrome tab; focus it for the join hold (`join_debounce_seconds`, default 12). After the session has started, a mute or a background tab does not by itself end it. `watch_google_meet = false` turns Meet detection off.

**Watcher never notices a Slack Huddle**
In `~/.meeting-notes/watch.log`, `Slack microphone not in use (slack.exe, ...)` means the desktop app is not holding the microphone. A Huddle in Chrome is not detected; use the Slack desktop app. The join still waits `join_debounce_seconds` (default 12) after the mic is seen, so a short clip does not start a session. If a Meet is already being recorded, the Huddle waits until that session ends. `watch_slack_huddle = false` turns Huddle detection off. `No channel or DM in the window title` means the recording will be named `Slack Huddle` plus the time.

**"global hotkey unavailable" on Windows**
The `keyboard` library couldn't install its low-level hook (often due to AV software). The tool falls back to Ctrl+C in the terminal. To use the hotkey, try running the terminal as administrator.

**Processing is slow**
Use a smaller Whisper model (`--whisper-model small` or `--whisper-model base`) for faster transcription at lower accuracy.

## License

MIT
