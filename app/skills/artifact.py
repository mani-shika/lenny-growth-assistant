"""The artifact skill: prompt, extraction, and safety gate.

An artifact is a document the user gets to *look at*, not a code block they have
to copy somewhere. So the model is asked for one fenced block with a title, and
this module is responsible for getting a renderable, safe payload out the other
side even when the model does not follow the format exactly - which small local
models frequently do not.

Extraction is deliberately forgiving, in this order:

1. A fenced ```html / ```markdown block (what we asked for).
2. Any fenced block, with the language inferred from its contents.
3. Raw HTML detected anywhere in the response.
4. The whole response, treated as Markdown.

Sanitisation is not forgiving. Every HTML artifact goes through the allowlist in
`sanitizer.py` before it is stored, and the report of what was stripped is
stored alongside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.skills.sanitizer import SanitiseReport, sanitize_html, wrap_document

FENCE_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)[ \t]*\r?\n(?P<body>.*?)```",
    re.DOTALL,
)
TITLE_RE = re.compile(r"^\s*(?:TITLE|Title)\s*:\s*(?P<title>.+?)\s*$", re.M)
MD_H1_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.M)
HTML_DOC_RE = re.compile(r"<(?:!doctype html|html|body|div|section|table|h1)\b", re.I)
HTML_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>")


@dataclass(slots=True)
class ExtractedArtifact:
    kind: str  # markdown | html
    title: str
    content: str  # sanitised + render-ready
    raw_content: str  # exactly what the model produced
    report: SanitiseReport
    # Prose the model wrote outside the fenced block, shown in the chat pane so
    # the transcript still reads as a conversation.
    commentary: str = ""
    # False when the model produced no fenced block and we had to infer the
    # format. Drives the single strict retry in the orchestrator.
    format_honoured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "sanitiser_report": self.report.to_dict(),
        }


def system_prompt(kind: str) -> str:
    """System prompt for artifact generation."""
    if kind == "html":
        body = """Produce a single, self-contained HTML document fragment.

Your entire reply must look exactly like this, with no other text at all:

TITLE: A short title
```html
<h1>A short title</h1>
<p>Real content here, written from the excerpts.</p>
```

RULES:
- Start with the TITLE: line. Then the ```html fence. Nothing else, ever.
- Never begin with "Here is" or any other sentence. The TITLE: line comes first.
- Real HTML tags only - <h1>, <p>, <ul>, <li>, <table>, <div>, <strong>.
  Markdown syntax such as ** or # does not render and must not appear.
- Style with a single <style> block or style attributes.
- No <script>, no event handlers (onclick and friends), no external resources
  (no CDN links, no remote images, no fonts, no fetch). They are stripped before
  rendering, so anything that relies on them will simply be missing.
- Readable at 360px wide and legible in dark mode."""
    else:
        body = """Produce a single Markdown document.

Your entire reply must look exactly like this, with no other text at all:

TITLE: A short title
```markdown
# A short title

Real content here, written from the excerpts.
```

RULES:
- Start with the TITLE: line. Then the ```markdown fence. Nothing else, ever.
- Never begin with "Here is" or any other sentence. The TITLE: line comes first.
- Use headings, bullets, tables and bold for structure.
- Do not embed raw HTML."""

    return f"""You create rendered artifacts from a conversation grounded in \
Lenny's Podcast transcripts.

{body}

GROUNDING: every factual claim comes from the numbered excerpts provided and
carries that excerpt's number in square brackets - for example: "Weekly active
use is the metric that mattered [2]." If the excerpts do not cover something,
leave it out rather than inventing it."""


def extract(response_text: str, preferred_kind: str = "markdown") -> ExtractedArtifact:
    """Pull a renderable artifact out of a model response."""
    raw_title = _find_title(response_text)
    block, lang, commentary = _find_block(response_text)
    format_honoured = block is not None

    if block is None:
        # No fence at all. Decide by inspecting the response itself.
        if HTML_DOC_RE.search(response_text):
            block, lang, commentary = response_text, "html", ""
        else:
            block, lang, commentary = response_text, "markdown", ""

    kind = _resolve_kind(lang, block, preferred_kind)
    body = block.strip()
    title = raw_title or _infer_title(body, kind)

    if kind == "html":
        safe, report = sanitize_html(body)
        content = wrap_document(safe, title)
    else:
        report = SanitiseReport()
        content = body

    return ExtractedArtifact(
        kind=kind,
        title=title[:300] or "Untitled artifact",
        content=content,
        raw_content=body,
        report=report,
        commentary=commentary.strip(),
        format_honoured=format_honoured,
    )


def _find_block(text: str) -> tuple[str | None, str, str]:
    """Return `(block_body, language, prose_outside_the_block)`."""
    matches = list(FENCE_RE.finditer(text))
    if not matches:
        return None, "", ""

    # Prefer an explicitly tagged block; otherwise take the longest one, which
    # is almost always the document rather than an incidental snippet.
    tagged = [m for m in matches if m.group("lang").lower() in {"html", "markdown", "md"}]
    chosen = (
        max(tagged, key=lambda m: len(m.group("body")))
        if tagged
        else max(matches, key=lambda m: len(m.group("body")))
    )

    commentary = (text[: chosen.start()] + "\n" + text[chosen.end() :]).strip()
    commentary = TITLE_RE.sub("", commentary).strip()
    return chosen.group("body"), chosen.group("lang").lower(), commentary


def _resolve_kind(lang: str, body: str, preferred: str) -> str:
    if lang == "html":
        return "html"
    if lang in {"markdown", "md"}:
        return "markdown"
    # An untagged block that is clearly markup should still render as HTML.
    if HTML_DOC_RE.search(body) or len(HTML_TAG_RE.findall(body)) >= 3:
        return "html"
    return "html" if preferred == "html" else "markdown"


def _find_title(text: str) -> str:
    match = TITLE_RE.search(text)
    if not match:
        return ""
    title = match.group("title").strip().strip("\"'")
    return "" if PREAMBLE_RE.match(title) else title


# "Here is a possible HTML one-pager summarising ..." is a preamble, not a title.
PREAMBLE_RE = re.compile(
    r"^(here('s| is| are)|below is|sure|certainly|okay|ok|i('ve| have)|this is)",
    re.I,
)


def _infer_title(body: str, kind: str) -> str:
    if kind == "markdown":
        match = MD_H1_RE.search(body)
        if match:
            return match.group("title").strip()
    match = re.search(r"<h1[^>]*>(?P<t>.*?)</h1>", body, re.I | re.S)
    if match:
        return re.sub(r"<[^>]+>", "", match.group("t")).strip()
    for line in body.splitlines():
        cleaned = line.strip().lstrip("#").strip().strip("*")
        if not cleaned:
            continue
        # Skip conversational preamble and anything that reads as a lead-in.
        if PREAMBLE_RE.match(cleaned) or cleaned.endswith(":"):
            continue
        return cleaned[:120]
    return "Untitled artifact"
