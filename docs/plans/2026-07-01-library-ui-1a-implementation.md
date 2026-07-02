# Meeting Library UI (Pillar 1a) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** A PySide6 tray app with a library window that lists all meetings, renders notes formatted, and provides a one-click "Copy for Notion" that pastes correctly into Notion with plain Ctrl/Cmd+V.

**Architecture:** Three new pure-Python modules (`library.py` data layer, `markdown_render.py` converter, `clipboard_payload.py` payload builder) are fully unit-tested; a thin Qt shell (`app.py`) wires them to a tray icon + window and is verified by a manual smoke checklist. Provider rule from the epic holds: nothing here touches or knows about `claude -p`.

**Tech Stack:** Python ≥3.9, PySide6 (new optional extra `app`), pytest (new extra `dev`), existing `summarizer._clean_for_notion` reused.

**Design docs:** [Epic](2026-07-01-meeting-app-epic.md) · [Pillar 1 PRD](2026-07-01-prd-meeting-library-ui.md)

> **Commit workflow note:** The user commits via **GitHub Desktop**, not the CLI.
> At each "Commit" step, STOP and ask the user to commit with the suggested
> message (or get explicit approval to run git yourself).

---

## Task 1: Test scaffolding + dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/` (directory; pytest needs no `__init__.py`)

**Step 1: Add extras to `pyproject.toml`**

Add after the `cuda` extra (keep existing extras untouched):

```toml
app = [
    "PySide6",
]
dev = [
    "pytest",
]
```

And add a GUI entry point section after `[project.scripts]`:

```toml
[project.gui-scripts]
meeting-notes-app = "meeting_notes.app:main"
```

(The `meeting_notes/app.py` module doesn't exist yet — that's fine; the entry
point resolves at run time, not install time.)

**Step 2: Install and verify**

Run: `pip install -e .[app,dev]`
Expected: installs PySide6 + pytest without errors.

Run: `python -m pytest`
Expected: `no tests ran` (exit code 5) — scaffolding works, nothing to run yet.

**Step 3: Commit**

Suggested message: `build: add app (PySide6) and dev (pytest) extras + gui entry point`

---

## Task 2: `library.py` — meeting data layer

Pure filesystem scanning, no Qt. Mirrors the parsing already in `cli.py:cmd_list`
(date/title from `YYYY-MM-DD_slug` folder names, most-recent-first) so the UI
and CLI agree.

**Files:**
- Create: `meeting_notes/library.py`
- Test: `tests/test_library.py`

**Step 1: Write the failing tests**

```python
import os

from meeting_notes.library import Meeting, list_meetings, load_notes


def _make_meeting(base, folder, notes=None):
    d = base / folder
    d.mkdir(parents=True)
    if notes is not None:
        (d / "notes.md").write_text(notes, encoding="utf-8")


def test_list_meetings_empty_when_dir_missing(tmp_path):
    assert list_meetings(str(tmp_path / "nope")) == []


def test_list_meetings_parses_and_sorts_desc(tmp_path):
    _make_meeting(tmp_path, "2026-06-30_client-kickoff", notes="x")
    _make_meeting(tmp_path, "2026-07-01_standup")
    (tmp_path / "stray-file.txt").write_text("ignore me")

    meetings = list_meetings(str(tmp_path))

    assert [m.folder for m in meetings] == [
        "2026-07-01_standup",
        "2026-06-30_client-kickoff",
    ]
    first = meetings[0]
    assert first.date == "2026-07-01"
    assert first.title == "Standup"
    assert first.has_notes is False
    second = meetings[1]
    assert second.title == "Client Kickoff"
    assert second.has_notes is True
    assert second.notes_path == os.path.join(str(tmp_path), second.folder, "notes.md")


def test_list_meetings_handles_folder_without_underscore(tmp_path):
    _make_meeting(tmp_path, "randomfolder")
    (m,) = list_meetings(str(tmp_path))
    assert m.title == "randomfolder"


def test_load_notes_reads_utf8(tmp_path):
    _make_meeting(tmp_path, "2026-07-01_sync", notes="**Summary**\n- ümlaut")
    (m,) = list_meetings(str(tmp_path))
    assert load_notes(m) == "**Summary**\n- ümlaut"
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_library.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'meeting_notes.library'`

**Step 3: Write the implementation**

`meeting_notes/library.py`:

```python
"""Meeting library data layer: scan ~/meeting-notes and load notes.

Pure filesystem code — no Qt — so it is unit-testable and shared between the
desktop app and the CLI's `list` command.
"""

import os
from dataclasses import dataclass

DEFAULT_BASE_DIR = os.path.expanduser("~/meeting-notes")


@dataclass(frozen=True)
class Meeting:
    folder: str      # e.g. "2026-07-01_standup"
    path: str        # absolute dir path
    date: str        # "2026-07-01" (from folder name)
    title: str       # "Standup" (from folder name)
    notes_path: str  # absolute path to notes.md (may not exist)
    has_notes: bool


def _parse_folder_name(folder):
    parts = folder.split("_", 1)
    date = parts[0] if parts else "unknown"
    title = parts[1].replace("-", " ").title() if len(parts) > 1 else folder
    return date, title


def list_meetings(base_dir=DEFAULT_BASE_DIR):
    """Return all meeting folders, most recent first (name-sorted desc)."""
    if not os.path.isdir(base_dir):
        return []
    meetings = []
    for entry in sorted(os.listdir(base_dir), reverse=True):
        path = os.path.join(base_dir, entry)
        if not os.path.isdir(path):
            continue
        date, title = _parse_folder_name(entry)
        notes_path = os.path.join(path, "notes.md")
        meetings.append(
            Meeting(
                folder=entry,
                path=path,
                date=date,
                title=title,
                notes_path=notes_path,
                has_notes=os.path.exists(notes_path),
            )
        )
    return meetings


def load_notes(meeting):
    """Return the raw markdown of a meeting's notes.md."""
    with open(meeting.notes_path, "r", encoding="utf-8") as f:
        return f.read()
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_library.py -v`
Expected: 4 passed

**Step 5: Commit**

Suggested message: `feat: add library data layer (scan meetings, load notes)`

---

## Task 3: DRY — `cli.py list` reuses `library.py`

`cmd_list` currently duplicates the folder-parsing logic. Point it at the new
data layer so there is exactly one implementation.

**Files:**
- Modify: `meeting_notes/cli.py` (`cmd_list`, currently ~lines 260–281)
- Test: `tests/test_cli_list.py`

**Step 1: Write the failing test**

```python
import meeting_notes.cli as cli


def test_cmd_list_uses_library(tmp_path, monkeypatch, capsys):
    d = tmp_path / "2026-07-01_standup"
    d.mkdir()
    (d / "notes.md").write_text("x", encoding="utf-8")
    (tmp_path / "2026-06-30_kickoff").mkdir()

    monkeypatch.setattr(cli, "OUTPUT_DIR", str(tmp_path))
    cli.cmd_list(None)

    out = capsys.readouterr().out
    assert "2026-07-01  Standup  (notes ready)" in out
    assert "2026-06-30  Kickoff  (no notes)" in out
    # most recent first
    assert out.index("Standup") < out.index("Kickoff")
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_list.py -v`
Expected: PASS is possible (current code produces the same output) — if it
passes, that's fine: it locks behavior *before* the refactor. Run it, confirm
green, then refactor under its protection.

**Step 3: Refactor `cmd_list`**

Replace the body of `cmd_list` in `meeting_notes/cli.py`:

```python
def cmd_list(_args):
    """List past meetings."""
    from meeting_notes.library import list_meetings

    meetings = list_meetings(OUTPUT_DIR)
    if not meetings:
        print("No meetings found.")
        return

    print("Past meetings:\n")
    for m in meetings:
        status = "notes ready" if m.has_notes else "no notes"
        print(f"  {m.date}  {m.title}  ({status})")
```

**Step 4: Run all tests**

Run: `python -m pytest -v`
Expected: all pass (library + cli tests)

**Step 5: Commit**

Suggested message: `refactor: cli list reuses library data layer`

---

## Task 4: `markdown_render.py` — markdown → semantic HTML

The core converter. Handles exactly our note subset: `**bold**`, `#`–`###`
headings (local-provider files), bullets nested by **tabs** (what
`_clean_for_notion` writes — see the PRD's "implementation trap") *or* spaces
(unit auto-detected like `_clean_for_notion` does), paragraphs. Output is
semantic (`<ul>/<li>/<strong>/<h1..3>/<p>`), never `<pre>`/`<code>`. Nested
lists are **valid HTML**: child `<ul>` lives inside its parent `<li>`.

**Files:**
- Create: `meeting_notes/markdown_render.py`
- Test: `tests/test_markdown_render.py`

**Step 1: Write the failing tests**

```python
from meeting_notes.markdown_render import md_to_html


def test_bold_paragraph():
    assert md_to_html("**Summary**") == "<p><strong>Summary</strong></p>"


def test_html_is_escaped():
    assert md_to_html("a < b & c") == "<p>a &lt; b &amp; c</p>"


def test_heading_levels():
    assert md_to_html("## Key Points") == "<h2>Key Points</h2>"
    assert md_to_html("# Title") == "<h1>Title</h1>"


def test_flat_bullets():
    assert md_to_html("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"


def test_tab_nested_bullets():
    md = "- parent\n\t- child\n\t\t- grandchild\n- sibling"
    assert md_to_html(md) == (
        "<ul><li>parent<ul><li>child<ul><li>grandchild</li></ul>"
        "</li></ul></li><li>sibling</li></ul>"
    )


def test_space_nested_bullets_unit_detected():
    md = "- parent\n  - child"
    assert md_to_html(md) == "<ul><li>parent<ul><li>child</li></ul></li></ul>"


def test_bold_inside_bullet():
    assert md_to_html("- a **key** point") == (
        "<ul><li>a <strong>key</strong> point</li></ul>"
    )


def test_blank_lines_skipped_and_sections_split():
    md = "**Summary**\n\n- a\n\n**Action Items**\n- b"
    assert md_to_html(md) == (
        "<p><strong>Summary</strong></p>\n<ul><li>a</li></ul>\n"
        "<p><strong>Action Items</strong></p>\n<ul><li>b</li></ul>"
    )


def test_never_emits_code_blocks():
    html = md_to_html("**Summary**\n- uses `backticks` and    spaces")
    assert "<pre" not in html and "<code" not in html
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_markdown_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'meeting_notes.markdown_render'`

**Step 3: Write the implementation**

`meeting_notes/markdown_render.py`:

```python
"""Markdown → semantic HTML for the app's known note subset.

Deliberately NOT a general markdown engine (YAGNI). Supports exactly what our
note providers emit: **bold**, # / ## / ### headings, bullets nested by TABS
(what summarizer._clean_for_notion writes) or spaces (unit auto-detected),
plain paragraphs. Output is always semantic — never <pre>/<code> — which is
what guarantees Notion renders a paste as blocks, not a code block.
"""

import html as _html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_BULLET = re.compile(r"^([ \t]*)- (.*)$")
_HEADING = re.compile(r"^(#{1,3}) +(.*)$")


def _inline(text):
    """Escape HTML, then apply **bold**."""
    return _BOLD.sub(r"<strong>\1</strong>", _html.escape(text, quote=False))


def _detect_indent_unit(lines):
    """First space-indented bullet defines the indent unit (like _clean_for_notion)."""
    for line in lines:
        m = re.match(r"^( +)- ", line)
        if m:
            return len(m.group(1))
    return 4


def _level(prefix, unit):
    """1-based nesting level from a whitespace prefix (tabs and/or spaces)."""
    tabs = prefix.count("\t")
    spaces = len(prefix) - tabs
    return 1 + tabs + spaces // unit


class _Item:
    def __init__(self, text):
        self.text = text
        self.children = []


def _tree(items):
    """Build a bullet tree from (level, text) pairs; tolerates level jumps."""
    root = _Item(None)
    stack = [(0, root)]
    for level, text in items:
        while stack[-1][0] >= level:
            stack.pop()
        node = _Item(text)
        stack[-1][1].children.append(node)
        stack.append((level, node))
    return root


def _render_list(node):
    if not node.children:
        return ""
    parts = ["<ul>"]
    for child in node.children:
        parts.append(f"<li>{_inline(child.text)}{_render_list(child)}</li>")
    parts.append("</ul>")
    return "".join(parts)


def md_to_html(markdown):
    """Convert our markdown subset to semantic HTML (one string, \n-joined blocks)."""
    lines = markdown.split("\n")
    unit = _detect_indent_unit(lines)
    out, bullets = [], []

    def flush_bullets():
        if bullets:
            out.append(_render_list(_tree(bullets)))
            bullets.clear()

    for line in lines:
        m = _BULLET.match(line)
        if m:
            bullets.append((_level(m.group(1), unit), m.group(2)))
            continue
        flush_bullets()
        h = _HEADING.match(line)
        if h:
            n = len(h.group(1))
            out.append(f"<h{n}>{_inline(h.group(2))}</h{n}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    flush_bullets()
    return "\n".join(out)
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_markdown_render.py -v`
Expected: 9 passed

**Step 5: Commit**

Suggested message: `feat: markdown->HTML converter (tab-aware nesting, semantic output)`

---

## Task 5: `clipboard_payload.py` — dual-format payload builder

Pure function returning `(plain_text, html)`; the Qt layer just pours it into
`QMimeData`. Plain slot keeps tab nesting (Notion's plain paste nests on tabs);
HTML slot is the semantic conversion of the same normalized markdown.

**Files:**
- Create: `meeting_notes/clipboard_payload.py`
- Test: `tests/test_clipboard_payload.py`

**Step 1: Write the failing tests**

```python
from meeting_notes.clipboard_payload import build_payload


RAW = "**Summary**\n- top **bold**\n    - nested\n"


def test_plain_slot_is_notion_normalized():
    plain, _ = build_payload(RAW)
    assert "\t- nested" in plain          # spaces -> tabs (_clean_for_notion)
    assert "**bold**" in plain            # markdown syntax intact


def test_html_slot_is_semantic():
    _, html = build_payload(RAW)
    assert "<ul><li>top <strong>bold</strong><ul><li>nested</li></ul></li></ul>" in html
    assert "<pre" not in html and "<code" not in html
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_clipboard_payload.py -v`
Expected: FAIL — module not found

**Step 3: Write the implementation**

`meeting_notes/clipboard_payload.py`:

```python
"""Build the dual-format clipboard payload for 'Copy for Notion'.

Pure (no Qt) so it is unit-testable; app.py pours the result into QMimeData,
and Qt maps it to CF_HTML (Windows) / public.html (macOS) natively.
"""

from meeting_notes.markdown_render import md_to_html
from meeting_notes.summarizer import _clean_for_notion


def build_payload(markdown):
    """Return (plain_text, html) for the clipboard.

    plain_text: markdown normalized for Notion's plain-text paste (tab nesting)
    html: semantic HTML for rich-paste targets — never <pre>/<code>
    """
    plain = _clean_for_notion(markdown)
    return plain, md_to_html(plain)
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest -v`
Expected: all tests pass (15 total)

**Step 5: Commit**

Suggested message: `feat: dual-format clipboard payload builder`

---

## Task 6: `app.py` — Qt tray + library window

The thin shell. All logic already lives in tested modules; this file is layout
+ signal wiring only, verified by the manual checklist in Task 7.

**Files:**
- Create: `meeting_notes/app.py`

**Step 1: Write the implementation**

`meeting_notes/app.py`:

```python
"""PySide6 desktop shell: tray icon + meeting library window (Pillar 1a).

Thin by design — data, rendering, and clipboard payload live in tested pure
modules. Per the epic's provider rule, this file knows nothing about claude -p.
"""

import os
import subprocess
import sys

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from meeting_notes.clipboard_payload import build_payload
from meeting_notes.library import list_meetings, load_notes
from meeting_notes.markdown_render import md_to_html


def _tray_icon():
    """Placeholder icon: solid square. Replaced by real branding later."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("#4a6cf7"))
    return QIcon(pixmap)


def open_folder(path):
    """Reveal a meeting folder in the OS file manager (per-OS shim, see PRD)."""
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    elif sys.platform == "win32":
        os.startfile(path)


class LibraryWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Meeting Notes")
        self.resize(900, 600)
        self._meetings = []
        self._current = None

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_select)

        self.viewer = QTextBrowser()

        self.copy_btn = QPushButton("Copy for Notion")
        self.copy_btn.clicked.connect(self._copy)
        self.copy_btn.setEnabled(False)

        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.clicked.connect(self._open_folder)
        self.folder_btn.setEnabled(False)

        actions = QHBoxLayout()
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.folder_btn)
        actions.addStretch()

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.viewer)
        right_layout.addLayout(actions)

        splitter = QSplitter()
        splitter.addWidget(self.list)
        splitter.addWidget(right)
        splitter.setSizes([280, 620])
        self.setCentralWidget(splitter)
        self.statusBar()  # create it so showMessage works

        self.refresh()

    def refresh(self):
        self._meetings = list_meetings()
        self.list.clear()
        for m in self._meetings:
            status = "notes ready" if m.has_notes else "no notes"
            QListWidgetItem(f"{m.date}   {m.title}   ({status})", self.list)

    def _on_select(self, row):
        if row < 0 or row >= len(self._meetings):
            return
        self._current = self._meetings[row]
        self.folder_btn.setEnabled(True)
        if self._current.has_notes:
            self.viewer.setHtml(md_to_html(load_notes(self._current)))
            self.copy_btn.setEnabled(True)
        else:
            self.viewer.setPlainText("No notes for this meeting yet.")
            self.copy_btn.setEnabled(False)

    def _copy(self):
        plain, html = build_payload(load_notes(self._current))
        mime = QMimeData()
        mime.setText(plain)
        mime.setHtml(html)
        QApplication.clipboard().setMimeData(mime)
        self.statusBar().showMessage(
            "Copied — paste into Notion with Ctrl+V / Cmd+V", 4000
        )

    def _open_folder(self):
        if self._current is not None:
            open_folder(self._current.path)

    def closeEvent(self, event):
        """Closing the window hides to tray; Quit lives in the tray menu."""
        event.ignore()
        self.hide()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    window = LibraryWindow()

    tray = QSystemTrayIcon(_tray_icon())
    menu = QMenu()
    open_action = QAction("Open Library", menu)

    def _open_library():
        window.refresh()
        window.show()
        window.raise_()
        window.activateWindow()

    open_action.triggered.connect(_open_library)
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(open_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.setToolTip("Meeting Notes")
    tray.show()

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

**Step 2: Reinstall so the gui-script resolves**

Run: `pip install -e .[app,dev]`
Expected: installs cleanly; `meeting-notes-app` now on PATH.

**Step 3: Quick launch sanity check**

Run: `meeting-notes-app` (or `python -m meeting_notes.app`)
Expected: window opens listing real meetings from `~/meeting-notes`; tray icon
visible. Close the window → app stays in tray. Quit via tray menu.

**Step 4: Run full test suite (regression)**

Run: `python -m pytest -v`
Expected: all pass (app.py has no unit tests by design — it's the thin shell).

**Step 5: Commit**

Suggested message: `feat: PySide6 tray app + meeting library window with Copy for Notion`

---

## Task 7: Manual acceptance checklist (release gate)

**Files:** none (verification only). The PRD makes the Notion paste an explicit
release gate — do not claim Pillar 1a done until the Windows column is checked.
(Mac column pending access to a Mac; tracked as an open item, not a blocker for
merging Windows-verified work.)

| # | Check | Windows | Mac |
|---|---|---|---|
| 1 | App launches; tray icon shows; window lists meetings most-recent-first with status | ☐ | ☐ |
| 2 | Selecting a meeting renders notes formatted (bold labels, nested bullets — no raw `**` or `#`) | ☐ | ☐ |
| 3 | Copy for Notion → plain **Ctrl/Cmd+V into Notion** renders blocks: bold labels, correctly nested bullets — **never a code block** | ☐ | ☐ |
| 4 | Same paste into a plain-text editor yields clean markdown (plain slot works) | ☐ | ☐ |
| 5 | Meeting without notes.md: viewer says so, Copy disabled, no crash | ☐ | ☐ |
| 6 | Open folder reveals the meeting directory | ☐ | ☐ |
| 7 | Close window → app stays in tray; Open Library re-opens with fresh list; Quit exits | ☐ | ☐ |

Also test check 3 with an **older meeting** (pre-dating this work) to confirm
tab-indented legacy notes.md files convert correctly.

**Commit** (docs update recording verification): `docs: record Pillar 1a acceptance results`

---

## Out of Scope (per PRD phasing — do not build)

- Notion API push, Preferences window, autostart (Pillar 1b)
- Reprocess button (needs background-thread work; add when pipeline wiring lands)
- Recording from the app, Google auth, auto-detect (Pillars 2–3)
- Real app icon/branding, packaging (PyInstaller) — separate distribution task
