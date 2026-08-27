"""Corpus parsing.

The source repository ships two shapes of markdown and both must end up in the
same retrievable form:

* ``podcasts/*.md`` - YAML frontmatter, then speaker turns that look like
  ``**Lenny Rachitsky** (00:12:34):`` followed by a paragraph.
* ``newsletters/*.md`` - YAML frontmatter, then ordinary prose with headings.

Parsing produces ``Segment`` objects that keep the speaker and timestamp where
they exist, because that metadata is what turns a citation from "somewhere in a
three-hour episode" into a deep link to the exact moment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# "**Speaker Name** (00:12:34):" - the transcript turn marker.
TURN_RE = re.compile(r"^\*\*(?P<speaker>[^*]{1,80})\*\*\s*\((?P<ts>\d{1,2}:\d{2}:\d{2})\):\s*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<yaml>.*?)\n---\s*\n?", re.DOTALL)
HEADING_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")

# The corpus is not uniform: roughly half the transcripts carry `post_url`
# (the Substack essay) and half carry `youtube_url` (the episode video). Both
# are legitimate canonical sources, so we take the first one present rather
# than silently losing a citation link for half the knowledge base.
URL_KEYS = ("post_url", "youtube_url", "url", "link", "episode_url")


@dataclass(slots=True)
class Segment:
    """The smallest unit of source text we keep provenance for."""

    text: str
    speaker: str | None = None
    timestamp: str | None = None
    heading: str | None = None

    @property
    def token_estimate(self) -> int:
        # ~4 characters per token is close enough for chunk budgeting and costs
        # nothing; exact counts would require a provider-specific tokeniser.
        return max(1, len(self.text) // 4)


@dataclass(slots=True)
class ParsedDocument:
    source_path: str
    title: str
    doc_type: str
    checksum: str
    guest: str | None = None
    published_at: str | None = None
    source_url: str | None = None
    word_count: int = 0
    segments: list[Segment] = field(default_factory=list)


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    try:
        meta = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError:
        # A malformed header must not cost us the document body.
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, raw[match.end() :]


def _parse_transcript_body(body: str) -> list[Segment]:
    """Split a speaker-labelled transcript into one Segment per turn."""
    segments: list[Segment] = []
    speaker: str | None = None
    timestamp: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            segments.append(Segment(text=text, speaker=speaker, timestamp=timestamp))
        buffer.clear()

    for line in body.splitlines():
        turn = TURN_RE.match(line.strip())
        if turn:
            flush()
            speaker = turn.group("speaker").strip()
            timestamp = turn.group("ts")
            continue
        buffer.append(line)
    flush()
    return segments


def _parse_prose_body(body: str) -> list[Segment]:
    """Split prose into paragraphs, carrying the nearest heading for context."""
    segments: list[Segment] = []
    heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            segments.append(Segment(text=text, heading=heading))
        buffer.clear()

    for line in body.splitlines():
        head = HEADING_RE.match(line)
        if head:
            flush()
            heading = head.group("title")
            continue
        if not line.strip():
            flush()
            continue
        buffer.append(line)
    flush()
    return segments


def parse_document(source_path: str, raw: str) -> ParsedDocument:
    """Parse one corpus file into metadata plus provenance-carrying segments."""
    meta, body = _split_frontmatter(raw)
    checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    declared_type = str(meta.get("type") or "").strip().lower()
    if declared_type in {"podcast", "newsletter"}:
        doc_type = declared_type
    else:
        doc_type = "podcast" if source_path.startswith("podcasts/") else "newsletter"

    if doc_type == "podcast":
        segments = _parse_transcript_body(body)
        # A transcript with no recognisable turn markers still has to be usable.
        if not segments:
            segments = _parse_prose_body(body)
    else:
        segments = _parse_prose_body(body)

    title = str(meta.get("title") or Path(source_path).stem.replace("-", " ").title())

    return ParsedDocument(
        source_path=source_path,
        title=title,
        doc_type=doc_type,
        checksum=checksum,
        guest=_opt_str(meta.get("guest")),
        published_at=_opt_str(meta.get("date")),
        source_url=_first_url(meta),
        word_count=int(meta.get("word_count") or len(body.split())),
        segments=segments,
    )


def iter_corpus_files(corpus_dir: Path) -> list[Path]:
    """All ingestible markdown, in a stable order so runs are reproducible."""
    if not corpus_dir.exists():
        return []
    files = [
        p
        for sub in ("podcasts", "newsletters")
        for p in sorted((corpus_dir / sub).glob("*.md"))
    ]
    # Support a flat directory too, so a hand-assembled corpus also works.
    if not files:
        files = sorted(corpus_dir.glob("*.md"))
    return files


def _first_url(meta: dict[str, Any]) -> str | None:
    """First populated URL field, in order of preference."""
    for key in URL_KEYS:
        value = _opt_str(meta.get(key))
        if value:
            return value
    return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
