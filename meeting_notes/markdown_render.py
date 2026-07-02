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
