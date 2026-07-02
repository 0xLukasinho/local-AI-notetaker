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
