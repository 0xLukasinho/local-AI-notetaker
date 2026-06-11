# Future Ideas / Productization Backlog

Parking lot for things we want to do when this app gets productized (UI, broader
user base). Not for immediate work — just so we don't forget.

## Clipboard → Notion paste UX

**Problem.** Pasting notes into Notion with real formatting depends on *what
format is on the clipboard*, and that trips up non-technical users:

- The pipeline's end-of-run clipboard copy works today because it puts **plain
  markdown** on the clipboard and Notion **auto-converts pasted markdown** into
  formatted blocks.
- But if a user opens the saved `notes.md` and copies it from an **editor**, it
  breaks: VS Code adds a syntax-highlighted **HTML "code" representation** that
  Notion prefers, so the paste shows up as literal/code. Notepad drops to plain
  text and Notion's auto-convert is inconsistent.
- **Current workaround (power-user only):** paste into Notion with
  `Ctrl+Shift+V` (paste as plain text), which discards the editor's HTML so
  Notion auto-converts the markdown.

**Why it matters for productization.** Most users won't know the `Ctrl+Shift+V`
trick. A real product needs a one-click "copy for Notion" (or share/export) that
puts the *right* bytes on the clipboard so a plain `Ctrl+V` always renders
correctly — no editor in the loop, no special paste.

**Possible directions** (decide once the UI exists — it has to fit the product):

- A **"Copy notes" button** in the UI that sets the clipboard to clean content
  (plain markdown for Notion's auto-convert, and/or proper Windows `CF_HTML`
  rich text so Ctrl+V renders in Notion, Docs, Slack, etc.).
- A headless `meeting-notes copy <meeting>` command that re-copies any past
  meeting's notes via the proven clipboard path (useful even pre-UI).
- Direct **Notion API push** (skip the clipboard entirely) — create the page in
  a chosen workspace/database automatically. More setup (auth + destination),
  but the most seamless for heavy Notion users.

**Status:** earmarked, not scheduled. No changes needed to the current output
file format.
