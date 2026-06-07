# Product Requirements Document: Meeting Notes Generator (Windows)

**Version:** 1.1
**Date:** 2026-06-07
**Status:** Implemented. Note generation uses Claude Code subscription (OAuth) rather than the Anthropic API.

---

## Executive Summary

A Windows port of the existing macOS Meeting Notes Generator. CLI tool that records meeting audio (system + microphone), transcribes it locally with Whisper, and generates structured notes by invoking the Claude Code CLI (which uses the user's Claude subscription via OAuth — no API key, no per-meeting cost). Same user experience as the Mac version, same file outputs, same prompt — adapted to Windows audio capture primitives.

---

## Relationship to the Mac Version

This port reuses the existing codebase rather than forking. The summarizer (`summarizer.py`) and transcriber (`transcriber.py`) are already platform-agnostic and stay unchanged. Only the audio recorder and a small number of CLI details need a Windows-specific path.

| Component | Mac | Windows |
|-----------|-----|---------|
| Audio capture | ffmpeg + AVFoundation | `soundcard` (WASAPI loopback + mic), mixed in Python |
| Transcription | openai-whisper (local) | openai-whisper (local) — unchanged |
| Note generation | Claude Code CLI subprocess (`claude -p`) | Claude Code CLI subprocess (`claude -p`) |
| Clipboard | `pbcopy` | `clip` |
| Stop hotkey | Cmd+Alt+Q (new global hotkey) | Ctrl+Alt+Q (new global hotkey) |
| Output dir | `~/meeting-notes/` | `%USERPROFILE%\meeting-notes\` |

Platform is detected at runtime via `sys.platform`. No fork, no duplication.

---

## Overview

### Purpose

Enable a Windows user to generate structured meeting notes and action items from Google Meet (or any meeting platform) calls without manually taking notes during the call.

### Core User Flow

1. Before the meeting, user runs: `meeting-notes start "Meeting Name"`
2. Tool records system audio (the other participants) and microphone (the user) into a single mixed WAV file.
3. User stops recording with the global hotkey **Ctrl+Alt+Q** (works even when the terminal is not focused).
4. Tool automatically:
   - Transcribes audio using local Whisper
   - Generates notes by invoking `claude -p --model claude-opus-4-7` as a subprocess (uses the user's Claude subscription)
   - Saves transcript + notes as markdown files
   - Copies notes to the Windows clipboard
5. User pastes notes into Notion (or other tools) with Ctrl+V.

---

## Data Locality & Security

The Mac PRD's security analysis applies unchanged. Summary for Windows:

| Step | Location | Network activity |
|------|----------|-----------------|
| Audio recording | Local (`%USERPROFILE%\meeting-notes\...\audio.wav`) | None |
| Transcription | Local (Whisper runs on CPU/GPU) | None (after one-time model download) |
| Note generation | Transcript sent to Claude via Claude Code CLI | HTTPS to Anthropic (subscription-authenticated) |
| Final storage | Local | None |

The transcript leaves the machine during note generation. Audio never does. Traffic goes through Claude Code's standard channel (OAuth-authenticated against the user's Max/Pro subscription); Anthropic's data-handling policy for that channel applies.

### File Structure

```
%USERPROFILE%\meeting-notes\
├── 2026-06-05_project-sync\
│   ├── audio.wav          # Auto-deleted after notes generation (use --keep-audio to retain)
│   ├── transcript.txt
│   └── notes.md
└── 2026-06-05_client-call\
    ├── audio.wav
    ├── transcript.txt
    └── notes.md
```

No API key required. Authentication is whatever Claude Code is logged in as on the machine (`claude login`).

---

## Technical Specification

### System Requirements

- **OS:** Windows 10 (1903+) or Windows 11
- **Python:** 3.9 or higher
- **External tools:** ffmpeg on PATH (required by Whisper for audio decoding); Claude Code CLI on PATH and logged in (used for note generation)
- **Audio:** A default playback device and a default recording device configured in Windows Sound settings
- **Disk:** ~2GB for Python + Whisper model; ~100–500MB per meeting (audio) + ~50KB transcript + ~5KB notes

### Dependency Stack

Existing (shared with Mac):

- `openai-whisper` — local speech-to-text
- Claude Code CLI (`claude`) — external dependency, invoked as a subprocess for note generation. No Python client library required.

New (Windows-only, declared as an optional extra in `pyproject.toml`):

- `soundcard` — native WASAPI loopback + mic capture, no driver install required
- `numpy` — for in-memory audio mixing (sum, gain, clip to int16)
- `keyboard` — global hotkey registration for stop signal

External:

- **ffmpeg** — required by Whisper. Install via `winget install ffmpeg` or `choco install ffmpeg`. Not used for recording on Windows (`soundcard` handles capture).

---

### Audio Recording (Windows)

**Library:** `soundcard` (PyPI)

**Why not ffmpeg here:** ffmpeg on Windows cannot capture system audio without a third-party virtual-device install (screen-capture-recorder, VB-Cable). `soundcard` does it natively via WASAPI loopback with zero user setup. ffmpeg stays in the dependency tree only because Whisper needs it to decode the WAV.

**Capture model:**

- A background `threading.Thread` opens two recorders:
  - `soundcard.default_speaker()` exposed as a loopback recorder (system audio — what other meeting participants are saying)
  - `soundcard.default_microphone()` (user's voice)
- Both are read in ~100ms blocks at 16kHz mono.
- Each block is summed sample-for-sample with light gain on the mic: `mix = clip(speaker + 0.7 * mic, -32768, 32767)` to keep the meeting content dominant and avoid clipping.
- The mix is appended to a streaming `wave` file (`audio.wav`, PCM16, 16kHz, mono) — same format the Mac version produces.
- A `threading.Event` ("stop") is checked each iteration. When set, the loop exits, flushes, closes the file.

**Stop signal:**

- Primary: global hotkey **Ctrl+Alt+Q** registered via the `keyboard` library. Works even when the terminal is not focused, so the user does not need to alt-tab out of their meeting app.
- Fallback: Ctrl+C still triggers a clean shutdown.
- If the `keyboard` library fails to register (e.g., blocked by AV or policy), the tool prints a warning at startup and falls back to "press Enter in the terminal to stop."

**Edge cases handled:**

- No default speaker or microphone configured → exit before recording starts with a hint to configure defaults in Windows Sound settings.
- Sample-rate mismatch between mic and speaker → resample mic to 16kHz inside the capture loop (`soundcard` returns float32; we convert and resample).
- Disk full / WAV write fails mid-recording → catch, close the file, surface a clear error, do not auto-delete.

---

### Transcription

Unchanged from the Mac version. `meeting_notes/transcriber.py` calls `whisper.load_model(...)` and transcribes. Same model sizes (`tiny`/`base`/`small`/`medium`/`large`). Same timestamped line format.

**Performance note:** Whisper on CPU is the same speed on Windows as on Mac for the same hardware class. A 60-minute meeting typically transcribes in 5–15 minutes with the `medium` model on a modern CPU. Users with an NVIDIA GPU and CUDA-enabled PyTorch will see substantially faster transcription; this is optional and not a setup requirement.

---

### Note Generation

`meeting_notes/summarizer.py` invokes the Claude Code CLI as a subprocess:

```
claude -p --model claude-opus-4-7
```

The prompt (template + transcript) is piped over stdin to avoid Windows command-line length limits. The CLI authenticates against the user's existing Claude subscription (OAuth via `claude login`), so usage counts against subscription quota rather than per-call API billing.

Default model is `claude-opus-4-7` (Max plan affords generous Opus limits); overridable via `--model` or the `MEETING_NOTES_MODEL` env var.

The prompt template (Summary / Action Items / Discussion Notes) is preserved verbatim, and the Notion-paste cleanup (`_clean_for_notion`) is unchanged. Clipboard copy uses `pbcopy` on Mac, `clip` on Windows.

**Why subprocess instead of an SDK:** Anthropic explicitly does not allow third-party tools (including the Claude Agent SDK) to authenticate against claude.ai subscriptions. The only realistic path to using a Max/Pro subscription for note generation is to invoke the user's own Claude Code installation, which is what this design does.

---

### Code Organization

```
meeting_notes/
├── cli.py              # OS-detects clipboard + hotkey, otherwise unchanged
├── recorder.py         # Thin dispatcher: imports the right backend
├── recorder_mac.py     # Existing ffmpeg/AVFoundation code (renamed)
├── recorder_windows.py # NEW: soundcard-based capture
├── transcriber.py      # Unchanged
├── summarizer.py       # Unchanged
└── summarizer_local.py # Unchanged (unused by default; kept for parity)
```

`recorder.py` exposes the same `record_audio(output_path)` and `stop_recording(handle)` API both backends implement. On Windows, the "handle" is the capture thread + stop Event + WAV handle bundled into a small object; on Mac it remains the `subprocess.Popen` for ffmpeg.

---

## User Interface

### Commands

```powershell
# Start recording
meeting-notes start "Meeting Name"

# Stop recording: press Ctrl+Alt+Q (global hotkey)
# Or Ctrl+C as a fallback

# List past meetings
meeting-notes list

# Re-run transcription and note generation on a past meeting
meeting-notes reprocess 2026-06-05_project-sync
meeting-notes reprocess 2026-06-05_project-sync --notes-only

# Keep the WAV around after notes succeed (default behavior deletes it)
meeting-notes start "Meeting Name" --keep-audio
```

### Flags

- `--model <name>` — Claude model (default `claude-opus-4-7`, env override `MEETING_NOTES_MODEL`)
- `--whisper-model <size>` — Whisper model size (default `medium`)
- `--notes-only` — on `reprocess`, skip transcription
- `--keep-audio` — **new**: skip the default auto-delete of `audio.wav` after a non-empty `notes.md` is successfully written

### Output Format

Identical to Mac. The prompt in `summarizer.py` already produces:

- **Summary** (bullet points)
- **Action Items** (omitted if none)
- **Discussion Notes** (nested bullets, compact shorthand)

Cleaned for clean Notion paste (tab indentation, no extra blank lines between bullets).

---

## Processing Time Estimates

Roughly the same as Mac for comparable hardware. For a 60-minute meeting on a typical Windows laptop (no GPU):

- Audio recording: real-time (meeting duration)
- Whisper transcription (`medium`, CPU): 5–15 minutes
- Note generation (Claude API): 30–60 seconds
- **Total post-meeting wait:** 6–16 minutes

With a CUDA-enabled GPU, transcription drops to 1–3 minutes.

---

## Out of Scope (v1)

Same exclusions as the Mac PRD:

- No GUI
- No real-time transcription
- No metadata tracking (attendees, tags, projects)
- No search
- No direct Notion API integration
- No speaker diarization
- English-only
- No cloud storage or backup
- No calendar integration
- No automatic meeting detection

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| `soundcard` loopback fails on a given audio driver | High | Surface a clear error; document fallback to VB-Cable for unusual setups |
| `keyboard` library blocked by AV / requires admin | Medium | Detect failure at startup, fall back to "press Enter to stop" with warning |
| ffmpeg not on PATH | Medium | Upfront check with `winget install ffmpeg` hint |
| Whisper transcription slow on CPU | Medium | Document `--whisper-model small` option; mention optional CUDA build of PyTorch |
| Sample-rate mismatch produces garbled audio | High | Resample to 16kHz inside capture loop, write WAV header accordingly |
| Mic dominates and drowns out meeting audio | Medium | Mix as `speaker + 0.7 * mic`, clip to int16 range |
| API key leakage | High | `.env` is gitignored; same posture as Mac version |
| Meeting contains PII/sensitive data | High | Same warning as Mac PRD — transcript goes to Anthropic; obtain IT approval |

---

## Implementation Phases

### Phase 1: Core Functionality (MVP)

- `recorder_windows.py` with `soundcard` loopback + mic capture, mixed to 16kHz mono WAV
- Global hotkey (Ctrl+Alt+Q) via `keyboard` library with Enter-to-stop fallback
- `recorder.py` dispatcher; rename current recorder to `recorder_mac.py`
- `cli.py` clipboard branch (`clip` vs `pbcopy`)
- `pyproject.toml` Windows extra (`pip install .[windows]`)
- Verify end-to-end on a real Google Meet call

### Phase 2: Quality of Life

- Auto-delete of `audio.wav` on success (with `--keep-audio` opt-out)
- Better error messages for missing audio devices
- README section for Windows setup (ffmpeg install, default-device hint)

### Phase 3: Future (Optional)

- Speaker diarization (if Whisper or a separate library makes this clean)
- Custom note templates
- Faster transcription path via `faster-whisper`

---

## Open Questions

1. (Resolved in v1.1: audio auto-deletes by default once notes are written; `--keep-audio` opts out.)
2. Should we ship a Whisper model preset based on detected hardware (CPU vs GPU) to avoid users picking the wrong size?
3. Is there an appetite later for a `meeting-notes serve` daemon that exposes the hotkey without re-registering per call?

---

## Appendix: Why These Library Choices

### Why `soundcard` for capture

- Native WASAPI loopback support out of the box, no driver install
- Pure Python API, no subprocess plumbing
- Maintained, MIT-licensed, widely used for exactly this use case
- Lets us mix mic + system audio in-process with precise control over gain and format

### Why not ffmpeg for capture on Windows

- ffmpeg's `dshow` backend cannot tap system audio without a third-party virtual device (screen-capture-recorder, VB-Cable). Both work, both require user installs. `soundcard` removes that friction entirely.

### Why `keyboard` for the stop hotkey

- A global hotkey is materially better UX than terminal-focused stop — the user lives inside Meet during the call, not the terminal.
- Cross-platform (works on Mac too, modulo accessibility permissions), so if we ever unify the Mac stop signal we have a path.
- Small, single-purpose library.
