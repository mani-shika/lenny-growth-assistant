"""In-process BM25 lexical index.

Why BM25 in Python rather than Postgres full-text search or an external
vector database:

* It is the component that **always works**. Dense retrieval needs Ollama to be
  reachable and the embedding model to be pulled; Postgres FTS needs the
  database. BM25 over an in-memory index needs neither, so the assistant
  degrades to "still answers, still cites" instead of "returns nothing".
* At this corpus size the numbers are unarguable: ~60 documents, a few thousand
  chunks, ~5 MB of text. The whole index is a few tens of MB of Python objects
  and a query is sub-millisecond.
* It is directly unit-testable with no infrastructure, which is what lets the
  retrieval tests run in CI on a clean checkout.

**When to replace it:** past roughly 10^5 chunks the memory and rebuild cost
stop being free. At that point move lexical scoring into Postgres
(`tsvector` + GIN + `ts_rank_cd`) and keep this module's interface - the
retriever only depends on ``search()`` returning ``(chunk_id, score)``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

K1 = 1.5  # term-frequency saturation
B = 0.75  # length normalisation

TOKEN_RE = re.compile(r"[a-z0-9]+")

# Closed-class words carry no retrieval signal but do inflate scores for long
# chunks. Kept deliberately small: domain words like "growth" or "retention"
# must never be stopped.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have
    he her him his how i if in into is it its me my of on or our out she so than
    that the their them then there these they this to was we were what when where
    which who will with would you your
    """.split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


@dataclass(slots=True)
class _Entry:
    chunk_id: str
    length: int
    freqs: Counter[str]


@dataclass(slots=True)
class LexicalHit:
    """A BM25 match plus the signal that says whether it is *about* the query.

    `score` ranks; `coverage` gates. They answer different questions and
    conflating them is how a retrieval system ends up citing sources for a
    question the corpus never addressed - see `BM25Index.coverage`.
    """

    chunk_id: str
    score: float
    coverage: float


@dataclass(slots=True)
class BM25Index:
    """A rebuildable BM25 index over chunk text."""

    entries: list[_Entry] = field(default_factory=list)
    postings: dict[str, list[int]] = field(default_factory=dict)
    avg_length: float = 0.0

    @property
    def size(self) -> int:
        return len(self.entries)

    @classmethod
    def build(cls, chunks: list[tuple[str, str]]) -> BM25Index:
        """Build from ``(chunk_id, text)`` pairs."""
        index = cls()
        for chunk_id, text in chunks:
            tokens = tokenize(text)
            if not tokens:
                continue
            position = len(index.entries)
            index.entries.append(
                _Entry(chunk_id=chunk_id, length=len(tokens), freqs=Counter(tokens))
            )
            for term in set(tokens):
                index.postings.setdefault(term, []).append(position)
        if index.entries:
            index.avg_length = sum(e.length for e in index.entries) / len(index.entries)
        return index

    def idf(self, term: str) -> float:
        """BM25 probabilistic IDF, floored at zero.

        A term absent from the corpus gets the maximum value rather than being
        skipped. That is what makes `coverage` able to notice that a question is
        about something the corpus has never heard of.
        """
        n = len(self.entries) or 1
        df = len(self.postings.get(term, ()))
        return max(0.0, math.log((n - df + 0.5) / (df + 0.5) + 1.0))

    def coverage(self, terms: list[str], entry: _Entry) -> float:
        """Fraction of the query's *information* this chunk actually contains.

        Weighted by IDF, so matching "product" counts for little and failing to
        match "sourdough" counts for a lot. Returns a value in [0, 1].

        This is the honest answer to "is there anything here?". Raw BM25 cannot
        answer it: a nonsense query still scores 7-8 against this corpus because
        one incidental common term matches, which is indistinguishable from a
        real question's score. Coverage separates them cleanly, because the
        rare terms a nonsense query is made of are exactly the ones missing.
        """
        if not terms:
            return 0.0
        total = 0.0
        matched = 0.0
        for term in set(terms):
            weight = self.idf(term)
            total += weight
            if entry.freqs.get(term):
                matched += weight
        return matched / total if total else 0.0

    def search(self, query: str, top_k: int = 20) -> list[LexicalHit]:
        """Return the best matches, highest score first."""
        terms = tokenize(query)
        if not terms or not self.entries:
            return []

        scores: dict[int, float] = {}

        for term in set(terms):
            postings = self.postings.get(term)
            if not postings:
                continue
            weight = self.idf(term)
            if weight == 0.0:
                continue
            for position in postings:
                entry = self.entries[position]
                tf = entry.freqs[term]
                norm = 1.0 - B + B * (entry.length / (self.avg_length or 1.0))
                scores[position] = scores.get(position, 0.0) + weight * (
                    tf * (K1 + 1.0) / (tf + K1 * norm)
                )

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [
            LexicalHit(
                chunk_id=self.entries[pos].chunk_id,
                score=score,
                coverage=self.coverage(terms, self.entries[pos]),
            )
            for pos, score in ranked
        ]
