"""Retrieval: parsing, chunking, BM25, the relevance gate, and fusion.

These run with no database and no model. They are the tests that would have
caught the two real bugs found while building this:

* the relevance gate applied to the fused RRF score, where it can never work;
* `post_url` treated as the only URL key, silently dropping the citation link
  for 34 of 60 documents.
"""

from __future__ import annotations

import pytest

from app.rag.chunker import chunk_segments
from app.rag.corpus import Segment, parse_document
from app.rag.lexical import BM25Index, tokenize
from app.rag.retriever import (
    reciprocal_rank_fusion,
    timestamped_url,
    _timestamp_to_seconds,
)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parses_frontmatter_and_speaker_turns(sample_transcript: str) -> None:
    doc = parse_document("podcasts/jane-doe.md", sample_transcript)

    assert doc.doc_type == "podcast"
    assert doc.guest == "Jane Doe"
    assert doc.published_at == "2026-03-14"
    assert doc.source_url == "https://www.lennysnewsletter.com/p/how-to-find-pmf"
    assert len(doc.segments) == 4
    assert doc.segments[1].speaker == "Jane Doe"
    assert doc.segments[1].timestamp == "00:00:20"
    assert "Retention is the only signal" in doc.segments[1].text


def test_parses_newsletter_prose_with_headings(sample_newsletter: str) -> None:
    doc = parse_document("newsletters/growth-loops.md", sample_newsletter)

    assert doc.doc_type == "newsletter"
    assert doc.guest is None
    assert any(s.heading == "Why loops beat funnels" for s in doc.segments)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("post_url", "https://example.com/post"),
        ("youtube_url", "https://example.com/post"),
        ("episode_url", "https://example.com/post"),
    ],
)
def test_accepts_every_url_key_the_corpus_uses(key: str, expected: str) -> None:
    """Regression: 34 of 60 real files use `youtube_url`, not `post_url`.

    Reading only `post_url` silently produced unlinked citations for over half
    the knowledge base - which looks like working software.
    """
    raw = f'---\ntitle: "T"\ntype: "podcast"\n{key}: "{expected}"\n---\n\nBody text.\n'
    assert parse_document("podcasts/x.md", raw).source_url == expected


def test_malformed_frontmatter_does_not_lose_the_body() -> None:
    raw = '---\ntitle: "unterminated\n  : : :\n---\n\n**A** (00:00:01):\nHello there.\n'
    doc = parse_document("podcasts/broken.md", raw)
    assert doc.segments, "a broken header must not cost us the transcript"


def test_checksum_is_content_addressed(sample_transcript: str) -> None:
    a = parse_document("podcasts/x.md", sample_transcript)
    b = parse_document("podcasts/x.md", sample_transcript)
    c = parse_document("podcasts/x.md", sample_transcript + "\nmore\n")
    assert a.checksum == b.checksum
    assert a.checksum != c.checksum


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_chunks_inherit_the_first_timestamp_in_the_group() -> None:
    segments = [
        Segment(text="A" * 200, speaker="Guest", timestamp="00:01:00"),
        Segment(text="B" * 200, speaker="Host", timestamp="00:02:00"),
    ]
    chunks = chunk_segments(segments, target_tokens=1000)

    assert len(chunks) == 1
    # The citation must point at where the passage *starts*.
    assert chunks[0].start_timestamp == "00:01:00"
    assert chunks[0].speakers == "Guest, Host"


def test_chunker_respects_the_token_budget() -> None:
    segments = [
        Segment(text="word " * 400, speaker="Guest", timestamp=f"00:0{i}:00")
        for i in range(5)
    ]
    chunks = chunk_segments(segments, target_tokens=600, max_tokens=1200, overlap=0)

    assert len(chunks) > 1
    assert all(c.token_estimate <= 1400 for c in chunks)


def test_oversized_segment_becomes_its_own_chunk_rather_than_being_dropped() -> None:
    segments = [
        Segment(text="short", speaker="Host", timestamp="00:00:01"),
        Segment(text="x " * 5000, speaker="Guest", timestamp="00:00:05"),
    ]
    chunks = chunk_segments(segments, target_tokens=500, max_tokens=1000)

    assert len(chunks) == 2
    assert "short" in chunks[0].text
    assert chunks[1].start_timestamp == "00:00:05"


def test_chunk_ordinals_are_contiguous_from_zero() -> None:
    segments = [
        Segment(text="word " * 300, speaker="G", timestamp="00:00:01") for _ in range(6)
    ]
    chunks = chunk_segments(segments, target_tokens=400)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


# --------------------------------------------------------------------------
# BM25 and the relevance gate
# --------------------------------------------------------------------------


CORPUS = [
    ("c1", "Retention is the only signal that matters for product market fit."),
    ("c2", "Pricing and willingness to pay are the fastest proxy for value."),
    ("c3", "Hiring your first product manager is mostly about judgement."),
    ("c4", "Growth loops compound because every user creates the next one."),
]


def test_tokenize_drops_stopwords_but_keeps_domain_words() -> None:
    tokens = tokenize("What is the retention of a growth product?")
    assert "the" not in tokens and "is" not in tokens
    assert {"retention", "growth", "product"} <= set(tokens)


def test_bm25_ranks_the_relevant_chunk_first() -> None:
    index = BM25Index.build(CORPUS)
    hits = index.search("pricing and willingness to pay", top_k=4)
    assert hits[0].chunk_id == "c2"


def test_lexical_search_does_not_stem_which_is_why_retrieval_is_hybrid() -> None:
    """Documents a known limitation rather than pretending it away.

    BM25 here matches surface forms: "price" does not match "Pricing", so a
    query using a different inflection ranks the wrong chunk. Adding a stemmer
    would fix this case and would also shift the coverage distribution the
    RETRIEVAL_MIN_COVERAGE threshold is calibrated against, so it is a change
    to make deliberately with a re-tune - not a one-line patch.

    In production this rarely bites, because the dense half of the hybrid
    handles morphological and synonym variation. This test exists so the
    behaviour is a recorded decision instead of a surprise.
    """
    index = BM25Index.build(CORPUS)
    inflected = index.search("how do I price my product", top_k=4)

    # "price" misses "Pricing"; the common word "product" decides the ranking.
    assert inflected[0].chunk_id == "c1"
    # The exact surface form finds it immediately.
    assert index.search("pricing", top_k=4)[0].chunk_id == "c2"


def test_empty_query_and_empty_index_are_safe() -> None:
    assert BM25Index.build(CORPUS).search("", top_k=4) == []
    assert BM25Index().search("anything", top_k=4) == []


def test_unmatched_rare_term_drives_coverage_down() -> None:
    """The gate's core property.

    A query built of terms the corpus has never seen must score near zero
    coverage even though BM25 still assigns it a respectable score from
    whatever common word happens to overlap.
    """
    index = BM25Index.build(CORPUS)

    on_topic = index.search("retention signal for product market fit", top_k=1)
    off_topic = index.search("sourdough hydration schedule for product", top_k=1)

    assert on_topic and on_topic[0].coverage > 0.7
    assert off_topic and off_topic[0].coverage < 0.45
    # And the point: BM25 score alone would not have separated them.
    assert off_topic[0].score > 0


def test_coverage_gate_separates_in_and_out_of_corpus() -> None:
    """Documents the tuning behind RETRIEVAL_MIN_COVERAGE = 0.45."""
    from app.core.config import settings

    index = BM25Index.build(CORPUS)
    in_corpus = ["retention product market fit", "growth loops compound"]
    out_of_corpus = ["quantum chromodynamics lagrangian", "timing belt honda civic"]

    for query in in_corpus:
        hits = index.search(query, top_k=4)
        best = max((h.coverage for h in hits), default=0.0)
        assert best >= settings.retrieval_min_coverage, query

    for query in out_of_corpus:
        hits = index.search(query, top_k=4)
        best = max((h.coverage for h in hits), default=0.0)
        assert best < settings.retrieval_min_coverage, query


# --------------------------------------------------------------------------
# Fusion
# --------------------------------------------------------------------------


def test_rrf_rewards_agreement_between_the_two_lists() -> None:
    lexical = [("a", 9.0), ("b", 8.0), ("c", 7.0)]
    dense = [("c", 0.1), ("a", 0.2), ("z", 0.3)]

    fused = dict(reciprocal_rank_fusion([lexical, dense]))

    # "a" is ranked by both lists; "b" and "z" by only one.
    assert fused["a"] > fused["b"]
    assert fused["a"] > fused["z"]


def test_rrf_is_scale_free() -> None:
    """Multiplying one list's scores must not change the fused order.

    This is the property that made RRF the right choice over weighted score
    blending: BM25 magnitudes and cosine distances live on incomparable scales.
    """
    lexical = [("a", 9.0), ("b", 8.0)]
    dense = [("b", 0.1), ("a", 0.2)]

    baseline = reciprocal_rank_fusion([lexical, dense])
    inflated = reciprocal_rank_fusion([[(k, v * 1000) for k, v in lexical], dense])

    assert [k for k, _ in baseline] == [k for k, _ in inflated]


def test_rrf_over_a_single_list_preserves_its_order() -> None:
    ranked = reciprocal_rank_fusion([[("a", 3.0), ("b", 2.0), ("c", 1.0)]])
    assert [k for k, _ in ranked] == ["a", "b", "c"]


# --------------------------------------------------------------------------
# Citation deep links
# --------------------------------------------------------------------------


def test_timestamp_to_seconds() -> None:
    assert _timestamp_to_seconds("00:19:43") == 1183
    assert _timestamp_to_seconds("01:00:09") == 3609
    assert _timestamp_to_seconds("nonsense") is None


def test_youtube_links_use_a_query_parameter_not_a_fragment() -> None:
    """A `#t=` fragment is ignored by YouTube - the link would land at 0:00."""
    url = timestamped_url("https://www.youtube.com/watch?v=abc", "00:19:43")
    assert url == "https://www.youtube.com/watch?v=abc&t=1183s"


def test_non_youtube_links_use_a_media_fragment() -> None:
    url = timestamped_url("https://www.lennysnewsletter.com/p/x", "00:02:00")
    assert url == "https://www.lennysnewsletter.com/p/x#t=120"


def test_missing_url_or_timestamp_is_handled() -> None:
    assert timestamped_url(None, "00:01:00") is None
    assert timestamped_url("https://example.com", None) == "https://example.com"
