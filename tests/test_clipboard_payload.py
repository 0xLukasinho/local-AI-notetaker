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
