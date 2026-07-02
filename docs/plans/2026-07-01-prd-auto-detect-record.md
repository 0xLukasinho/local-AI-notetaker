# PRD — Pillar 3: Auto-detect + Record Popup

**Version:** 0.1 (draft for review)
**Date:** 2026-07-01
**Status:** Planning. Part of the [Meeting Notes Desktop App epic](2026-07-01-meeting-app-epic.md).
**Depends on:** [Pillar 1](2026-07-01-prd-meeting-library-ui.md) (app shell + recorder) and [Pillar 2](2026-07-01-prd-google-account-integration.md) (calendar access).

---

## Executive Summary

The headline feature: the app **notices when a meeting is happening** — either
from the connected Google Calendar or from a **spontaneous Google Meet** — and
surfaces a **popup with a Record button**, so the user captures the call with one
click instead of remembering to start a CLI beforehand. Recording reuses the
existing capture pipeline; when the meeting ends, the normal
transcribe → notes → library flow runs.

This is the **highest-risk pillar**: it needs a reliable background watcher, some
genuinely per-OS detection code, OS permissions, and it exposes the known
**Mac system-audio** gap. We scope it honestly: **calendar-based detection is the
reliable core; spontaneous-Meet detection is best-effort** with clearly
documented limits.

---

## Goals

- A background watcher (in the existing tray app) that detects:
  - **Scheduled meetings** with a Meet link from connected calendars, and
  - **Spontaneous Google Meets** the user joins outside the calendar (best-effort).
- A **non-intrusive popup** at meeting start: *"Recording '<title>'? [Record] [Dismiss]"*.
- One-click **Record** → existing recorder captures system + mic audio.
- Auto-finalize on meeting end (or manual stop) → transcribe → notes → library.
- Sensible controls: snooze, "don't ask for this meeting," per-account on/off.

## Non-Goals

- No auto-recording without consent — the popup (or an explicit opt-in default)
  always gates capture. Recording people has legal/ethical weight.
- No transcription/notes changes — reuses Pillar 1's pipeline.
- No support for non-Meet platforms in v1 (Zoom/Teams noted as future).

---

## Detection Strategy (two tiers, honest about reliability)

### Tier A — Calendar-based (reliable, primary)

Using Pillar 2's `calendar.readonly` access, the watcher polls upcoming events
(e.g. every 60s, plus a near-term refresh), extracts `hangoutLink` / Meet URLs,
and when an event with a Meet link reaches its **start time**, fires the popup.
This is deterministic and cross-platform (pure API) — the backbone of the
feature.

- Handles: scheduled 1:1s, recurring standups, anything with a Meet link on the
  calendar.
- Misses: ad-hoc Meets started from `meet.google.com/new` with no event.

### Tier B — Spontaneous Meet (best-effort, per-OS)

To catch ad-hoc Meets, we detect that **a Google Meet is active on the machine**.
There is **no Google API for "a Meet just started"**, so this is necessarily
heuristic and OS-specific. Candidate signals (in preference order):

1. **Window/tab title detection.** Enumerate window titles for a Meet signature
   ("Meet - …", "Google Meet"):
   - **Windows:** `EnumWindows` via `pywin32`/`ctypes` (no special permission).
   - **macOS:** `CGWindowListCopyWindowInfo` (Quartz) — **requires Screen
     Recording permission** to read other apps' window titles on modern macOS.
   *Limitation:* browsers usually reflect only the **active tab** in the window
   title, so a backgrounded Meet tab may be invisible. Reliable for the Meet
   **desktop app** and for a foregrounded tab; unreliable for background tabs.
2. **Audio-activity heuristic (fallback).** Detect that a capture/render session
   consistent with a call is active. Lower precision; used only to *prompt*
   ("Are you in a meeting?"), never to auto-record.

Tier B is explicitly **best-effort**: we ship Tier A as the guarantee and layer B
on top, logging clearly in the PRD and UI that spontaneous detection can miss
background tabs. We will **not** inspect page content or use a browser extension
in v1 (privacy + scope).

---

## Background Watcher & Popup

- Runs inside the Pillar 1 tray process (no separate service to install).
- **State machine per candidate meeting:** `detected → prompted → recording →
  finalizing → done` (or `dismissed`/`snoozed`). Dedup so calendar + Meet-window
  signals for the same call don't double-prompt (match on Meet URL / time window).
- **Popup** is a small always-on-top Qt window (or native notification with
  actions), showing title + source and **Record / Dismiss / Snooze**. Auto-times
  out to "Dismiss" so it never blocks the user.
- **Recording** calls the existing `recorder.record_audio(...)`; **stop** is
  triggered by meeting end (calendar end time / Meet window closing) or the
  existing global hotkey / a tray "Stop" item. Finalization reuses the exact
  transcribe→notes→save path, then the meeting appears in the Library (Pillar 1),
  tagged `source: auto` in `meta.json`.

---

## Cross-Platform Considerations

| Concern | Mac | Windows | Shared? |
|---|---|---|---|
| Calendar polling (Tier A) | Google API | Google API | ✅ shared |
| Popup / notification | Qt | Qt | ✅ shared |
| State machine + dedup + finalize | Python | Python | ✅ shared |
| **Meet window detection (Tier B)** | Quartz `CGWindowList` (Screen-Recording perm) | Win32 `EnumWindows` | ❌ per-OS adapter |
| **Audio capture** | AVFoundation / **ScreenCaptureKit** | WASAPI loopback (`pyaudiowpatch`) | ❌ existing per-OS split |
| Autostart (watcher on login) | LaunchAgent | Registry `Run` / Startup | ❌ tiny per-OS shim |

Per-OS surface is confined to detection, audio, and autostart — each behind a
stable interface (`detect_active_meet() -> Meeting | None`, the existing
`record_audio`, `enable_autostart()`).

---

## The Mac System-Audio Gap (called out explicitly)

Windows already captures **system audio** (other participants) via WASAPI
loopback with no user setup. The **current Mac recorder captures a single
AVFoundation device index** — it does **not** capture system audio without a
virtual device (e.g. BlackHole) plus an aggregate device. For an
auto-record feature that "just works," that gap is unacceptable long-term.

**Plan:**
- Evaluate **ScreenCaptureKit** (macOS 13+) which can capture **system audio
  natively without a virtual device** — the modern, install-free path. Wrap it
  behind the existing `record_audio` interface so nothing upstream changes.
- **Fallback:** document BlackHole + aggregate-device setup for older macOS.
- This work is a **prerequisite for Pillar 3 on Mac** and is tracked as its own
  risk; it does not affect Pillars 1–2.

---

## Consent, Privacy & Legal

- **No silent recording.** Default is prompt-to-record; even an "auto-record"
  preference is opt-in and clearly indicated (tray state + recording indicator).
- Meet **window-title** reads (Tier B) are used only to detect *that* a meeting
  is happening — never page content. On macOS this requires the user to grant
  Screen Recording permission, requested with a clear explanation.
- Recording consent is the user's responsibility; we surface a first-run notice
  about one/two-party consent norms.

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Mac system-audio capture** (see above) | High | ScreenCaptureKit path behind existing interface; BlackHole fallback; gates Mac Pillar-3 release. |
| Spontaneous-Meet detection misses background tabs | Medium | Ship Tier A as the guarantee; label Tier B best-effort in UI; Meet desktop app + foreground tab work. |
| macOS Screen-Recording permission friction/denial | Medium | Feature degrades to Tier A only if denied; clear onboarding; never hard-fail. |
| Double-prompting (calendar + window signal) | Medium | Dedup on Meet URL + time window in the state machine. |
| Popup annoyance / prompt fatigue | Medium | Snooze, "don't ask for this meeting," per-account toggle, auto-dismiss timeout. |
| Battery/CPU from polling & window enumeration | Low-Med | 60s calendar poll; throttle window scans; back off when idle/asleep. |
| Recording the wrong window / false positive | Medium | Show detected title in popup for user confirmation before capture. |
| Global hotkey library blocked (as noted in existing PRD) | Medium | Reuse existing fallback (tray Stop / Enter); already handled in recorder. |

---

## Acceptance Criteria

- With a Google account connected, a calendar meeting with a Meet link triggers
  the Record popup at start time (Tier A), on both OSes.
- Clicking **Record** captures audio and, on stop, produces notes that appear in
  the Library tagged `source: auto`.
- A spontaneous Meet **desktop app** (or foreground tab) triggers the popup
  (Tier B) on both OSes where permissions allow; missing a background tab is a
  documented, accepted limitation.
- On macOS, system audio (remote participants) is captured via the chosen path
  (ScreenCaptureKit or documented BlackHole setup).
- No recording ever starts without an explicit user action (or the clearly-
  labeled opt-in auto-record).

---

## Phasing

1. **3a — Calendar-driven (Tier A):** watcher + popup + record wiring + finalize,
   using existing Windows audio; **plus the Mac ScreenCaptureKit audio work** as
   its parallel prerequisite.
2. **3b — Spontaneous detection (Tier B):** per-OS Meet-window adapters + dedup.
3. **3c — Polish:** snooze/ignore controls, auto-record opt-in, autostart-on-login.

---

## Open Questions

1. ScreenCaptureKit vs BlackHole as the **default** Mac system-audio path — how
   many users are on macOS 13+? (Ties to the epic's min-OS question.)
2. For Tier B, is the **Meet desktop app + foreground tab** coverage enough for
   v1, or do we need a browser extension later for background tabs?
3. Auto-stop trigger: trust calendar end time, Meet-window-closed, silence
   detection, or a combination?
4. Default behavior: always prompt, or offer an opt-in "auto-record my calendar
   meetings" mode from the start?
5. Zoom/Teams detection — future pillar, or fold in once the Meet pattern proves out?
