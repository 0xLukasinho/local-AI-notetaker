# PRD — Pillar 1: Meeting Library UI

**Version:** 0.1 (draft for review)
**Date:** 2026-07-01
**Status:** Planning. Part of the [Meeting Notes Desktop App epic](2026-07-01-meeting-app-epic.md).
**Depends on:** nothing (ships standalone). **Prerequisite for:** nothing.

---

## Executive Summary

A mostly-dormant **PySide6 tray app** with a single window that lets a user
browse every past meeting, read its notes **rendered correctly in-app**, and —
the headline — **copy the notes with one click so a plain `Ctrl/Cmd+V` into
Notion always renders as real blocks**, never a code block or flat text. An
optional **direct Notion push** creates the page via API for connected users,
skipping the clipboard entirely.

This pillar exists first because it (a) finally resolves the origin problem and
(b) is the smallest leap from today's code — it wraps the existing pipeline and
`~/meeting-notes/` files in a window.

---

## Goals

- One window listing all meetings (most recent first), openable from the tray.
- Notes rendered **as formatted content** in-app (headings, bold, nested bullets).
- A **Copy button that the app fully controls**, producing clipboard content that
  pastes correctly into Notion via plain paste — on Mac and Windows.
- Optional **"Push to Notion"** for users who connect a Notion workspace.
- Zero required setup for the copy path (Notion push is opt-in).

## Non-Goals (this pillar)

- No recording/auto-detection (Pillar 3) and no Google auth (Pillar 2).
- No editing of notes in-app (read + copy/export only, v1).
- No search/tagging/attendees UI (future; `meta.json` leaves room).
- No provider (Claude/ChatGPT) selection UI — tracked at the epic level.

---

## Why This Solves the Notion Problem (the crux)

The paste bug was never about our content — it was that **a third-party editor
owned the clipboard write** (VS Code injects syntax-highlighted HTML → Notion
makes a code block; a markdown editor strips `#`/`-` on copy → flat text). The
fix is to **remove the editor from the loop**: the app writes the clipboard
itself, with bytes we choose.

Two things make this robust *and* cross-platform:

1. **We control the source.** The Copy button reads the raw `notes.md` and
   normalizes it (reusing/extending `summarizer._clean_for_notion`), so the
   markdown syntax Notion needs is intact — no editor to mangle it.
2. **Qt writes native rich clipboard formats for free.** `QClipboard` +
   `QMimeData` let us attach **both**:
   - `setText(markdown)` — clean plain markdown (Notion auto-converts on paste), and
   - `setHtml(html)` — clean **semantic** HTML (`<ul>`, `<strong>`, `<p>`; plus
     `<h1>–<h3>` for providers that emit headings) generated from the markdown.

   Qt maps `setHtml` to the correct OS clipboard format automatically —
   **CF_HTML on Windows, `public.html`/`NSPasteboard` on Mac** — with no per-OS
   code from us. Notion prefers HTML and renders our clean semantic HTML as real
   blocks; because our HTML is semantic (never `<pre>`/`<code>`), it is
   **never** a code block. Apps that prefer plain text still get valid markdown.

   > **Note on the actual note format:** the Claude prompt
   > (`summarizer.py`) deliberately produces **no headings** — the three
   > sections use **bold labels** (`**Summary**`, `**Action Items**`,
   > `**Discussion Notes**`) plus nested bullets. The converter/renderer must
   > handle that shape as the primary case; heading support exists for other
   > providers (e.g. `summarizer_local.py` emits `## Key Points`).

This is exactly the capability the CLI lacked (raw `clip`/`pbcopy` are
plain-text only, and hand-rolling CF_HTML/NSPasteboard is the risky native work
we rejected). Inside Qt it is a few lines, shared across both OSes.

### Copy content pipeline

```
notes.md (raw markdown)
   │  _clean_for_notion()  (existing: tabs for nesting, tidy blank lines)
   ▼
normalized markdown ──► QMimeData.setText(markdown)
   │  markdown→HTML (small, dependency-light converter; whitelist of h/ul/ol/li/strong/em/p)
   ▼
semantic HTML     ──►  QMimeData.setHtml(html)
   ▼
QClipboard.setMimeData(...)   →  Ctrl/Cmd+V in Notion = formatted blocks
```

> **Implementation trap (flagged during design review):** `_clean_for_notion`
> converts bullet nesting to **tab indentation** (that's what Notion's
> plain-text paste nests on), and saved `notes.md` files already carry tabs.
> Standard markdown converters expect *space*-indented nesting and may flatten
> or mis-nest tab-indented lists. The markdown→HTML converter (and the in-app
> renderer, which shares it) **must be tested against tab-indented input** —
> or normalize tabs→spaces before conversion while keeping tabs in the
> plain-text clipboard slot.

We will verify the rendered result in Notion for both paste styles on both OSes
(this is a listed acceptance test, not an assumption).

---

## User Flows

**Read & copy (the 90% case).**
1. User clicks the tray icon → **Open Library**.
2. Window shows the meeting list; user clicks a meeting.
3. Detail pane renders the notes formatted.
4. User clicks **Copy for Notion** → toast "Copied — paste into Notion."
5. User pastes with plain `Ctrl/Cmd+V`; it renders correctly.

**Re-copy an older meeting (the original pain).** Same flow — the "I already
copied something else" problem disappears because copying is one click in the
app, not a hunt through an editor.

**Push to Notion (opt-in).**
1. In Preferences, user connects Notion (OAuth) and picks a target database/page.
2. On a meeting, user clicks **Push to Notion** → app creates a page via API →
   toast links to the new page. No clipboard involved.

---

## UI Structure

- **Tray icon** (always running, low footprint): menu with *Open Library*,
  *Record…* (Pillar 3, later), *Preferences*, *Quit*.
- **Library window** (opened on demand, closes to tray):
  - **Left:** meeting list — date, title, status chip (`notes ready` / `no
    notes`), most-recent-first. Reuses the logic already in `cli.py:cmd_list`.
  - **Right:** detail pane — rendered notes (`QTextBrowser`, lightweight, no
    Chromium) + action bar: **Copy for Notion**, **Push to Notion** (enabled
    only when connected), **Open folder**, **Reprocess** (calls existing
    pipeline).
- **Preferences:** AI provider status (Claude/ChatGPT CLI detected?), Notion
  connection, storage location, autostart toggle.

Rendering: convert `notes.md` markdown → HTML and display in `QTextBrowser`.
`QTextBrowser` supports a rich subset (headings, bold, lists) — sufficient for
our three-section note format. If fidelity ever falls short we can swap to
`QWebEngineView`, but we avoid its Chromium weight unless needed.

---

## Notion API Push (optional, phased within this pillar)

- **Auth:** Notion OAuth (public integration) or an internal-integration token
  pasted in Preferences for v1 simplicity; store via `keyring`.
- **Destination:** user picks a target database or parent page once.
- **Conversion:** markdown → Notion blocks (`heading_3`, `bulleted_list_item`
  with nesting, `to_do` for action items, inline `bold`). Pure HTTPS — shared
  across OSes.
- **Idempotency:** record the created `page_id` in `meta.json` so re-push updates
  rather than duplicates (or prompts).
- Ships **after** the copy path within Phase 1; copy is the MVP.

---

## Cross-Platform Considerations

| Concern | Mac | Windows | Shared? |
|---|---|---|---|
| Window/tray/toast | Qt | Qt | ✅ one codebase |
| Notes render | `QTextBrowser` | `QTextBrowser` | ✅ |
| Clipboard plain+HTML | Qt → `public.html` | Qt → CF_HTML | ✅ (Qt maps it) |
| Notion API push | HTTPS | HTTPS | ✅ |
| Open-folder action | `open` | `explorer` | tiny per-OS shim |

The only per-OS line in this pillar is "reveal in file manager." Everything
else — including the clipboard rich-format handling that defeated the CLI — is
shared.

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Notion changes how it interprets pasted HTML | Medium | We ship *both* HTML and plain markdown; plain-markdown auto-convert is the proven fallback. Acceptance test locks current behavior. |
| `QTextBrowser` under-renders some markdown (e.g. tables) | Low | Our note format is headings/bold/nested bullets only; escalate to `QWebEngineView` only if needed. |
| markdown→HTML converter adds a heavy dep | Low | Use a small pure-Python converter or a minimal hand-rolled one over our known-narrow markdown subset. |
| Notion API auth/setup friction | Medium | Copy path needs zero setup; push is strictly opt-in. |

---

## Acceptance Criteria

- Library lists all `~/meeting-notes/*` folders, most-recent-first, with status.
- Opening a meeting renders its notes formatted (bold section labels + nested
  bullets — the actual Claude note format; headings render too where present).
- **Copy for Notion → plain `Ctrl+V` (Windows) and `Cmd+V` (Mac) into Notion
  yields correctly formatted blocks, never a code block** — verified manually on
  both OSes as a release gate.
- With Notion connected, **Push to Notion** creates a correctly formatted page.
- App runs from the tray with the window closed; reopening is instant.

---

## Phasing

1. **1a:** Tray + library window + notes rendering + **Copy for Notion**. (MVP —
   resolves the origin problem.)
2. **1b:** Notion API push + Preferences (Notion connect, autostart, provider
   status).

---

## Open Questions

1. `QTextBrowser` vs `QWebEngineView` for rendering — start light, escalate only
   if fidelity demands it. Confirm the note format never needs tables/code.
2. Notion auth: public OAuth integration vs pasted internal token for v1?
3. Should **Copy** offer a format toggle (rich vs plain-markdown) for non-Notion
   targets, or auto-attach both and let the target choose? (Leaning: attach both.)
4. Do we surface the transcript in the UI, or notes only? (Leaning: notes only,
   with "Open folder" for the rest.)
