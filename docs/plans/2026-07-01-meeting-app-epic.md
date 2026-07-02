# Epic: Meeting Notes Desktop App (Mac + Windows)

**Version:** 0.1 (draft for review)
**Date:** 2026-07-01
**Status:** Planning. No code to be written until the three feature PRDs below are approved.
**Owner:** Lukasinho

> This epic is the umbrella document for turning the current CLI (`meeting-notes`)
> into a productized, cross-platform desktop app. It defines the shared vision,
> the architecture all three features share, and the cross-platform strategy.
> The detail lives in three feature PRDs:
>
> 1. [Meeting Library UI](2026-07-01-prd-meeting-library-ui.md) — the window, notes rendering, and the definitive "copy into Notion" fix.
> 2. [Google Account Integration](2026-07-01-prd-google-account-integration.md) — connect one or more Google accounts.
> 3. [Auto-detect + Record Popup](2026-07-01-prd-auto-detect-record.md) — watch the calendar / detect Meets and offer one-click recording.

---

## Executive Summary

Today `meeting-notes` is a cross-platform Python CLI: it records system + mic
audio, transcribes locally with faster-whisper, and generates structured notes
by invoking the user's Claude Code CLI (`claude -p`) against their Claude
subscription. It works, but it is driver-by-terminal and assumes a technical
user.

This epic evolves it into a **mostly-dormant desktop app** that lives in the
system tray. The app keeps the existing Python pipeline intact as its engine and
wraps it in a **PySide6 (Qt)** shell so that non-technical users get:

- a window to browse, read, and **reliably copy** past meeting notes (finally
  fixing the Notion-paste problem that motivated this work),
- the ability to **sign in with their own Claude / ChatGPT subscriptions** the
  same sanctioned way the CLI does today (by driving the vendor's official CLI),
- and — the headline feature — **automatic detection of meetings** (from the
  calendar or a spontaneous Google Meet) with a **one-click Record popup**, so
  they never miss capturing a call.

The guiding constraint throughout: **maximize shared code across Mac and
Windows, and confine per-OS code to the few places the operating system forces
our hand** (audio capture, window/Meet detection, autostart). Everything else —
pipeline, UI, integrations — is one Python codebase.

---

## Problem & Motivation

The immediate trigger was a real, recurring failure: a user finishes a meeting,
means to paste the notes into Notion, gets distracted, copies something else,
then re-copies the notes from a text editor — and Notion turns them into a code
block (VS Code emits syntax-highlighted HTML) or flat text (a markdown editor
strips the syntax on copy). The root cause is that **the app doesn't control the
clipboard write when a third-party editor is in the loop**, and a CLI cannot fix
that.

Zooming out, that single bug is a symptom of the product ceiling: a terminal
tool can't own the moments that matter — the copy, the "a meeting just started,"
the "let me glance at last week's notes." An app can. This epic is the response.

See [`docs/future-ideas.md`](../future-ideas.md) for the original productization
backlog entry that seeded this.

---

## The Three Pillars

| # | Pillar | What the user gets | Relative effort |
|---|--------|--------------------|-----------------|
| 1 | **Meeting Library UI** | Tray app + window: list meetings, read notes rendered correctly, one-click copy that always pastes right into Notion (+ optional direct Notion push). | Medium — closest to today's code; *fully resolves the origin problem*. |
| 2 | **Google Account Integration** | Connect one or more Google accounts (OAuth); read calendar for Pillar 3; identity for future per-user features. | Medium — standard desktop OAuth; no audio/OS-capture risk. |
| 3 | **Auto-detect + Record Popup** | Background watcher: calendar meetings and spontaneous Meets surface a Record popup. Never miss a recording. | High — background service, per-OS detection, permissions, and the hardest audio edge cases. |

The pillars are deliberately **independent**: Pillar 1 ships value with zero
Google/auth work; Pillar 2 is a prerequisite only for Pillar 3; Pillar 3 is the
most novel and riskiest. This lets us sequence honestly (see Roadmap).

---

## Shared Architecture

The app is layered so the **OS-specific surface is as thin as possible**. Only
the shaded pieces below are per-OS; everything else is one shared Python
codebase.

```
                    ┌───────────────────────────────────────────┐
                    │              PySide6 App Shell              │   shared
                    │  tray icon · notes window · popups · prefs  │  (one Qt
                    │  (QClipboard/QMimeData handles clipboard    │   codebase)
                    │   formats natively on Win + Mac)            │
                    └───────────────┬───────────────────────────┘
                                    │ direct in-process calls
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
┌───────▼────────┐        ┌─────────▼─────────┐       ┌─────────▼──────────┐
│ Core pipeline  │        │   Integrations    │       │  Platform adapters │
│  (shared)      │        │    (shared)       │       │   (per-OS, thin)   │  ← only OS code
│ transcriber.py │        │ Claude CLI (subp.)│       │ recorder_windows   │
│ summarizer.py  │        │ ChatGPT CLI(subp.)│       │ recorder_mac       │
│ notes cleanup  │        │ Google OAuth      │       │ meet-detect win/mac│
│ storage/model  │        │ Notion API        │       │ tray/hotkey/autostart│
└────────────────┘        └───────────────────┘       └────────────────────┘
```

**Layer responsibilities**

- **Core pipeline (shared).** The existing `transcriber.py`, `summarizer.py`,
  and the `~/meeting-notes/` file model — unchanged in spirit. The app calls
  these directly instead of through `cli.py`.
- **App shell (shared, PySide6).** Tray icon, the library window, the record
  popup, preferences. One Qt codebase renders on both OSes with native tray and
  notification support. Crucially, **Qt's clipboard (`QClipboard` +
  `QMimeData`) writes the correct native clipboard formats on each OS** (CF_HTML
  on Windows, `public.html`/`NSPasteboard` on Mac) with no per-OS code from us —
  this is what makes the Notion copy fix free and cross-platform (see Pillar 1).
- **Integrations (shared).** Vendor-CLI subprocess auth (Claude/ChatGPT),
  Google OAuth, Notion API — all pure Python/HTTPS/subprocess, identical on both
  OSes.
- **Platform adapters (per-OS, isolated).** The only place we accept duplicated
  code, behind stable interfaces: audio capture (already split
  `recorder.py` → `recorder_windows.py` / `recorder_mac.py`), Meet/window
  detection, tray/hotkey quirks, and login-item autostart.

### Reuse scorecard (the north star)

| Concern | Shared? | Per-OS code required |
|---|---|---|
| Transcription (faster-whisper) | ✅ shared | none |
| Note generation (vendor CLI subprocess) | ✅ shared | none |
| Notes cleanup / markdown normalization | ✅ shared | none |
| UI (tray, window, popups, prefs) | ✅ shared (Qt) | none |
| Clipboard copy (plain + HTML) | ✅ shared (Qt) | **none** — Qt maps to CF_HTML / NSPasteboard |
| Notion API push | ✅ shared | none |
| Google OAuth + token storage | ✅ shared (`keyring` abstracts the OS keystore) | none |
| Calendar polling | ✅ shared | none |
| **Audio capture** | ❌ per-OS | WASAPI loopback (Win) vs AVFoundation/ScreenCaptureKit (Mac) |
| **Meet / window detection** | ❌ per-OS | Win32 window enum vs macOS CGWindowList/Accessibility |
| **Autostart (login item)** | ❌ per-OS | Registry/Startup (Win) vs LaunchAgent (Mac) |

Three narrow per-OS seams, all already conceptually isolated. That is the whole
argument for the stack choice.

---

## Subscription Authentication Model (Claude & ChatGPT)

A first-class requirement: users bring **their own AI subscriptions**, exactly
as the CLI does today.

- **The only sanctioned path is to drive the vendor's official CLI as a
  subprocess.** Anthropic explicitly forbids third-party apps (including their
  own Agent SDK) from authenticating against claude.ai subscriptions; the
  supported route is to invoke the user's Claude Code install (`claude -p`),
  which owns the OAuth session. This is what `summarizer.py` already does via
  `shutil.which("claude")`.
- **ChatGPT extends the same pattern.** OpenAI's Codex CLI signs in with a
  ChatGPT plan; the app detects and drives it the same way (`shutil.which` →
  subprocess with prompt over stdin).
- **Implication for architecture:** "log in with Claude / ChatGPT" is not an
  in-app OAuth screen we build — it is *"is the vendor CLI installed and logged
  in?"* plus a provider abstraction (`summarizer` grows a small strategy that
  picks Claude vs ChatGPT vs, later, a raw API key). This is portable Python,
  identical on both OSes, and keeps us on the right side of vendor ToS.

### AI Provider Strategy & Monetization (researched, deliberately deferred)

We considered how the AI backend affects sharing/productization and concluded it
does **not** constrain the current design, provided one rule holds.

**Conclusion:**

- **Iteration 1 stays on `claude -p`.** Note quality is the product; Claude is
  currently the best and we won't risk regressing it. (An earlier Llama-based
  local attempt produced poor notes; newer open-weight models — Qwen, DeepSeek —
  are worth re-evaluating later.)
- **Both paths are wanted eventually:** a **bundled local model** (free, private,
  no token bill) *and* **connect-your-own-Claude subscription**. Local is
  deferred mainly because CPU-only inference is slow and past local quality was
  poor — not because anything blocks it.
- **Swapping/adding a provider later is a single-module change** behind the
  existing `generate_notes(transcript, …)` seam. The blast radius is one file +
  a Preferences toggle. In fact a working local provider **already exists**:
  `summarizer_local.py` is a complete Ollama backend (default `qwen3:32b`) with
  the identical signature — it is simply not wired into the CLI today. The
  `LocalModelProvider` below is a formalization of it, not new ground.
- **The one rule that keeps this cheap:** no provider-specific behavior
  (subprocess calls, prompt quirks, output parsing) may leak past that seam. As
  long as everything Claude-specific lives inside the provider module, there is
  no lock-in and no rework when we add local/API providers.
- **Monetization is unresolved and needs its own research** (BYO-subscription
  vs BYO-API-key vs managed API you bill for vs cheap hosted APIs like DeepSeek —
  the last with a real transcript-privacy caveat). It is **not** load-bearing for
  the current architecture, so we move ahead without settling it now.

**Formalization (small, additive):** treat note generation as a `NotesProvider`
interface with pluggable backends — `SubscriptionCLIProvider` (claude/codex),
`CloudAPIProvider` (Anthropic/OpenAI/DeepSeek key), `LocalModelProvider`
(Ollama/llama.cpp). Iteration 1 implements only the first; the interface keeps
every business model open.

A dedicated provider-selection / monetization PRD is **out of scope for this
epic's first iteration** but is the natural home for the research above.

---

## Stack Decision & Rationale

**Chosen: Python core + PySide6 (Qt) desktop shell.** Alternatives considered:
web UI (Tauri/Electron) + Python sidecar; native per-OS (SwiftUI + WinUI).

Rationale (recorded here so future contributors see the "why"):

1. **Least total and least per-OS code.** The unavoidable OS-specific code is
   audio capture — dictated by OS audio APIs regardless of UI stack, and already
   isolated. A web-UI stack would add a second runtime + an IPC bridge (more
   total code) without removing any per-OS code; native-per-OS doubles the UI.
2. **Single language end-to-end.** The pipeline, the vendor-CLI subprocess auth,
   and the UI all live in Python — no JS/Python split, no IPC contract.
3. **Subscription model fits natively.** Driving `claude`/`codex` subprocesses
   is trivial and already proven in Python.
4. **The clipboard fix comes free.** Qt's clipboard writes native rich formats
   on both OSes, which is precisely the capability a CLI lacked.

Accepted trade-off: a Qt UI is more utilitarian than a web app. Since the UI is
**minimal and rarely opened by design**, visual polish is the lowest-priority
axis. PySide6 is LGPL (fine for commercial distribution); PyInstaller bundles a
single app per OS.

---

## Data Model & Storage

Unchanged from today, extended additively:

```
~/meeting-notes/                      (Windows: %USERPROFILE%\meeting-notes\)
├── 2026-07-01_project-sync/
│   ├── audio.wav        # auto-deleted after notes (existing behavior)
│   ├── transcript.txt
│   ├── notes.md
│   └── meta.json        # NEW (additive): source (calendar/manual), attendees,
│                        #   google account, meet url, notion page id, timestamps
└── ...
```

`meta.json` is optional and backward-compatible — the library UI treats its
absence gracefully (older meetings still list and open). App-level config
(connected Google accounts, Notion target, provider choice) lives in a per-user
config dir (`platformdirs`), with secrets in the OS keystore via `keyring`.

---

## Packaging & Distribution

- **PyInstaller** produces a `.app` (Mac) and a folder/`.exe` (Windows) from one
  spec. Whisper models and ffmpeg remain external (downloaded/installed on first
  run) to keep the bundle small — same posture as today.
- **Signing/notarization** (Apple notarization; Windows code signing) is
  required for a non-scary install but is a distribution concern, tracked as an
  open question, not a feature PRD.
- The **CLI stays shipped** alongside the app (same package) for power users and
  automation; the app and CLI share the core modules.

---

## Roadmap / Phasing

Recommended sequence (each phase independently shippable):

1. **Phase 1 — Meeting Library UI (Pillar 1).** Tray + window + rendered notes +
   the copy fix. Closes the origin problem, no auth dependencies. Optional
   Notion push can trail within the phase.
2. **Phase 2 — Google Account Integration (Pillar 2).** OAuth + token storage +
   account management. Ships value on its own (calendar-aware library) and is the
   prerequisite for Phase 3.
3. **Phase 3 — Auto-detect + Record Popup (Pillar 3).** Background watcher,
   detection, popup, wiring to the recorder. Highest risk; benefits from 1 & 2
   being solid.

We are writing **all three PRDs now** (this request) so the architecture is
coherent before any coding, but implementation follows the phases above.

---

## Cross-Cutting Risks

| Risk | Impact | Mitigation / where addressed |
|---|---|---|
| **Mac system-audio capture is weaker than Windows** — current Mac recorder captures a single AVFoundation device; true system audio needs a virtual device (BlackHole) or ScreenCaptureKit (macOS 13+). | High | Pillar 3 risk section owns this; evaluate ScreenCaptureKit as the native, install-free path; document BlackHole fallback. Not blocking Pillars 1–2. |
| Vendor CLI ToS / behavior changes (Claude/ChatGPT) | High | Keep provider behind a strategy; degrade to "CLI not found → guide install"; never embed subscription tokens ourselves. |
| App-signing/notarization friction | Medium | Distribution task; budget time before public release. |
| PyInstaller + faster-whisper/CUDA bundling quirks | Medium | Keep models/ffmpeg external; test packaged builds early on both OSes. |
| Feature creep across three pillars | Medium | Independent PRDs + phased delivery; YAGNI in each. |

---

## Open Questions (epic-level)

1. Do we distribute app + CLI as one installer, or split? (Leaning: one package.)
2. Minimum supported OS versions — especially macOS floor if we adopt
   ScreenCaptureKit (13+) for system audio.
3. Do we need an in-app updater, or rely on store/website downloads?
4. Provider selection UX (Claude vs ChatGPT vs API key) — its own mini-PRD after
   Pillar 1?

---

## Document Map

- **This epic** — vision, shared architecture, cross-platform strategy, phasing.
- [Pillar 1 — Meeting Library UI](2026-07-01-prd-meeting-library-ui.md)
- [Pillar 2 — Google Account Integration](2026-07-01-prd-google-account-integration.md)
- [Pillar 3 — Auto-detect + Record Popup](2026-07-01-prd-auto-detect-record.md)
