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
