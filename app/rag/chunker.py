"""Turning parsed segments into retrievable chunks.

Chunking strategy and why it is this one:

* **Group whole segments, never split mid-turn.** A speaker turn is the
  smallest self-contained unit of meaning in an interview. Splitting one in
  half produces chunks that quote a subject without its verb.
* **Target ~1,000 tokens.** Large enough that a guest's full argument survives
  in one chunk; small enough that eight of them fit a local model's context
  alongside the conversation.
* **One segment of overlap.** Cheap insurance for answers that straddle a
  boundary, without the storage blow-up of token-level overlap.
* **Inherit the first timestamp in the group.** That is what a citation links
  to, so it must be the moment the passage *starts*.

A single oversized segment (a long monologue, or prose with no paragraph
breaks) becomes its own chunk rather than being dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.rag.corpus import Segment

TARGET_TOKENS = 1000
MAX_TOKENS = 1600
OVERLAP_SEGMENTS = 1


@dataclass(slots=True)
class BuiltChunk:
    ordinal: int
    text: str
    speakers: str
    start_timestamp: str | None
    token_estimate: int


def _render(segments: list[Segment]) -> str:
    """Render a group back to text, keeping speaker labels the model can use."""
    parts: list[str] = []
    for seg in segments:
        if seg.speaker:
            stamp = f" ({seg.timestamp})" if seg.timestamp else ""
            parts.append(f"{seg.speaker}{stamp}: {seg.text}")
        elif seg.heading:
            parts.append(f"[{seg.heading}] {seg.text}")
        else:
            parts.append(seg.text)
    return "\n\n".join(parts)


def _finalise(group: list[Segment], ordinal: int) -> BuiltChunk:
    speakers = sorted({s.speaker for s in group if s.speaker})
    stamps = [s.timestamp for s in group if s.timestamp]
    text = _render(group)
    return BuiltChunk(
        ordinal=ordinal,
        text=text,
        speakers=", ".join(speakers)[:400],
        start_timestamp=stamps[0] if stamps else None,
        token_estimate=max(1, len(text) // 4),
    )


def chunk_segments(
    segments: list[Segment],
    target_tokens: int = TARGET_TOKENS,
    max_tokens: int = MAX_TOKENS,
    overlap: int = OVERLAP_SEGMENTS,
) -> list[BuiltChunk]:
    """Group segments into chunks of roughly ``target_tokens``."""
    chunks: list[BuiltChunk] = []
    group: list[Segment] = []
    running = 0

    for seg in segments:
        size = seg.token_estimate

        # An individual segment larger than the hard cap gets its own chunk;
        # flush whatever was accumulating first so ordering stays faithful.
        if size >= max_tokens:
            if group:
                chunks.append(_finalise(group, len(chunks)))
                group, running = [], 0
            chunks.append(_finalise([seg], len(chunks)))
            continue

        if group and running + size > target_tokens:
            chunks.append(_finalise(group, len(chunks)))
            group = group[-overlap:] if overlap else []
            running = sum(s.token_estimate for s in group)

        group.append(seg)
        running += size

    if group:
        chunks.append(_finalise(group, len(chunks)))

    # Overlap can make the tail chunk a pure duplicate of its predecessor.
    if len(chunks) > 1 and chunks[-1].text == chunks[-2].text:
        chunks.pop()
    return chunks
