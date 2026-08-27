"""Hybrid retrieval: BM25 + dense vectors, fused with reciprocal rank fusion.

Design notes
------------
**Why hybrid.** Lexical search nails proper nouns and jargon ("PMF", "Superhuman
score", a guest's name) that embeddings blur together. Dense search catches
paraphrase ("how do I know people want this?" -> product/market fit). Product and
growth questions arrive in both shapes, so we run both and fuse.

**Why RRF rather than weighted score blending.** BM25 scores are unbounded and
corpus-dependent; cosine similarities sit in [-1, 1]. Normalising them against
each other requires constants that need re-tuning whenever the corpus changes.
RRF only consumes *rank*, so it is scale-free and has one interpretable knob.

**Relevance gating happens before fusion, not after.** RRF scores encode rank
agreement, not relevance - a single-list top hit always scores 1/(k+1) whether
the match was perfect or accidental - so thresholding the fused score cannot
detect "the corpus does not cover this". The gate therefore runs on each list's
own signal: IDF-weighted query coverage for lexical, cosine distance for dense.
RRF is left to do only the thing it is good at, which is ordering.

**Degradation.** If dense retrieval is unavailable the fusion runs over the
lexical list alone. The caller cannot tell the difference except through the
`strategy` field, which is logged and surfaced in /api/health.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sqlalchemy import select, text as sql_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger, timed
from app.db.models import Chunk, Document
from app.rag.embeddings import embed_query
from app.rag.lexical import BM25Index

log = get_logger(__name__)

# RRF damping. 60 is the value from the original Cormack et al. formulation and
# behaves well when the two lists disagree, which is exactly our case.
RRF_K = 60
# How deep to go in each individual list before fusing.
CANDIDATE_DEPTH = 30
# Cosine distance above which a dense neighbour is treated as unrelated.
# Measured against nomic-embed-text over this corpus: in-corpus questions
# topped out at 0.396 and out-of-corpus questions bottomed out at 0.505, so the
# midpoint separates them with room on both sides. Re-measure if the embedding
# model changes - the scale is model-specific.
MAX_DENSE_DISTANCE = 0.45


@dataclass(slots=True)
class Citation:
    """Everything the UI needs to render a source without another query."""

    chunk_id: str
    document_title: str
    doc_type: str
    guest: str | None
    published_at: str | None
    source_url: str | None
    speakers: str
    timestamp: str | None
    score: float
    excerpt: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    citation: Citation


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk]
    strategy: str  # hybrid | lexical | dense | empty
    lexical_hits: int
    dense_hits: int

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def as_context(self, max_chars: int = 24_000) -> str:
        """Render chunks as the numbered evidence block given to the model.

        Numbering is 1-based and stable, because the system prompt instructs
        the model to cite as [1], [2] and we map those back to `chunks`.
        """
        parts: list[str] = []
        budget = max_chars
        for i, chunk in enumerate(self.chunks, start=1):
            cite = chunk.citation
            stamp = f" at {cite.timestamp}" if cite.timestamp else ""
            header = f"[{i}] {cite.document_title}"
            if cite.guest:
                header += f" (guest: {cite.guest})"
            header += stamp
            body = chunk.text
            block = f"{header}\n{body}"
            if len(block) > budget:
                block = block[: max(0, budget)]
            if not block.strip():
                break
            parts.append(block)
            budget -= len(block)
            if budget <= 0:
                break
        return "\n\n---\n\n".join(parts)


class Retriever:
    """Owns the in-memory lexical index and runs hybrid search against it."""

    def __init__(self) -> None:
        self._bm25 = BM25Index()
        self._loaded = False

    @property
    def is_ready(self) -> bool:
        return self._loaded and self._bm25.size > 0

    @property
    def chunk_count(self) -> int:
        return self._bm25.size

    async def load(self, session: AsyncSession) -> int:
        """(Re)build the lexical index from the database.

        Called at startup and after ingestion. Failure leaves the previous
        index in place rather than clearing it, so a transient database blip
        cannot take retrieval down.
        """
        try:
            with timed(log, "retrieval.index_build") as fields:
                rows = (await session.execute(select(Chunk.id, Chunk.text))).all()
                index = BM25Index.build([(r[0], r[1]) for r in rows])
                fields["chunks"] = index.size
            self._bm25 = index
            self._loaded = True
            return index.size
        except SQLAlchemyError as exc:
            log.error("retrieval.index_build_failed", error=str(exc)[:300])
            return self._bm25.size

    async def search(
        self,
        session: AsyncSession,
        query: str,
        top_k: int | None = None,
    ) -> RetrievalResult:
        top_k = top_k or settings.retrieval_top_k

        raw_lexical = self._bm25.search(query, top_k=CANDIDATE_DEPTH)
        raw_dense = await self._dense_search(session, query, limit=CANDIDATE_DEPTH)

        # Gate each list on its own relevance signal before fusing.
        lexical = [
            hit for hit in raw_lexical if hit.coverage >= settings.retrieval_min_coverage
        ]
        dense = [
            (cid, distance)
            for cid, distance in raw_dense
            if distance <= MAX_DENSE_DISTANCE
        ]

        if lexical and dense:
            strategy = "hybrid"
        elif lexical:
            strategy = "lexical"
        elif dense:
            strategy = "dense"
        else:
            strategy = "empty"

        fused = reciprocal_rank_fusion(
            [[(h.chunk_id, h.score) for h in lexical], dense]
        )
        chunks = await self._hydrate(session, fused[:top_k])

        log.info(
            "retrieval.search",
            query_chars=len(query),
            strategy=strategy,
            lexical_candidates=len(raw_lexical),
            lexical_hits=len(lexical),
            dense_candidates=len(raw_dense),
            dense_hits=len(dense),
            best_coverage=round(max((h.coverage for h in raw_lexical), default=0.0), 3),
            returned=len(chunks),
        )
        return RetrievalResult(
            chunks=chunks,
            strategy=strategy if chunks else "empty",
            lexical_hits=len(lexical),
            dense_hits=len(dense),
        )

    async def _dense_search(
        self, session: AsyncSession, query: str, limit: int
    ) -> list[tuple[str, float]]:
        vector = await embed_query(query)
        if vector is None:
            return []
        try:
            # pgvector's <=> is cosine distance; smaller is closer. We only use
            # the ordering, so no conversion to similarity is needed.
            rows = (
                await session.execute(
                    sql_text(
                        "SELECT id, embedding <=> CAST(:q AS vector) AS distance "
                        "FROM chunks WHERE embedding IS NOT NULL "
                        "ORDER BY distance ASC LIMIT :k"
                    ),
                    {"q": str(vector), "k": limit},
                )
            ).all()
        except SQLAlchemyError as exc:
            log.warning(
                "retrieval.dense_failed",
                error=str(exc)[:300],
                impact="lexical-only for this query",
            )
            return []
        return [(row[0], float(row[1])) for row in rows]

    async def _hydrate(
        self, session: AsyncSession, scored: list[tuple[str, float]]
    ) -> list[RetrievedChunk]:
        """Load chunk + document rows for the fused ids, preserving rank order."""
        if not scored:
            return []
        ids = [cid for cid, _ in scored]
        rows = (
            await session.execute(
                select(Chunk, Document)
                .join(Document, Chunk.document_id == Document.id)
                .where(Chunk.id.in_(ids))
            )
        ).all()
        by_id = {chunk.id: (chunk, doc) for chunk, doc in rows}

        out: list[RetrievedChunk] = []
        for chunk_id, score in scored:
            found = by_id.get(chunk_id)
            if not found:
                continue  # index is stale relative to the DB; skip quietly
            chunk, doc = found
            out.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    text=chunk.text,
                    citation=Citation(
                        chunk_id=chunk.id,
                        document_title=doc.title,
                        doc_type=doc.doc_type,
                        guest=doc.guest,
                        published_at=doc.published_at,
                        source_url=timestamped_url(doc.source_url, chunk.start_timestamp),
                        speakers=chunk.speakers,
                        timestamp=chunk.start_timestamp,
                        score=round(score, 6),
                        excerpt=_excerpt(chunk.text),
                    ),
                )
            )
        return out


def reciprocal_rank_fusion(
    rankings: list[list[tuple[str, float]]], k: int = RRF_K
) -> list[tuple[str, float]]:
    """Fuse ranked lists by rank alone.

    Each list contributes ``1 / (k + rank)`` for every id it ranks. Ids that
    appear in both lists therefore outrank ids that either list loved alone,
    which is precisely the behaviour we want from lexical+dense agreement.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (item_id, _) in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def timestamped_url(url: str | None, timestamp: str | None) -> str | None:
    """Deep-link a citation to the moment it came from.

    The syntax is host-specific, and getting it wrong means the link silently
    lands at 0:00 - which looks like a working citation but is not one.
    YouTube wants a `t=<seconds>s` query parameter; everything else gets a
    media fragment, which Substack's player honours and other hosts ignore
    harmlessly.
    """
    if not url or not timestamp:
        return url
    seconds = _timestamp_to_seconds(timestamp)
    if seconds is None:
        return url
    if "youtube.com" in url or "youtu.be" in url:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}t={seconds}s"
    return f"{url}#t={seconds}"


def _timestamp_to_seconds(timestamp: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", timestamp.strip())
    if not match:
        return None
    hours, minutes, secs = (int(g) for g in match.groups())
    return hours * 3600 + minutes * 60 + secs


def _excerpt(text: str, limit: int = 260) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


# Process-wide singleton. The index is read-mostly and rebuilt explicitly, so a
# single shared instance avoids paying the build cost per request.
retriever = Retriever()
