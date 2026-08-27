"""Skill routing, follow-up query rewriting, and the Ship 30 validator."""

from __future__ import annotations

import pytest

from app.agent.orchestrator import build_search_query
from app.agent.router import route
from app.agent.types import ChatMessage, Role, Skill
from app.skills import ship30


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "How do you know when you have product/market fit?",
        "What separates a great PM from a good one?",
        "Why did Duolingo's growth stall?",
        "Who should I hire first, a designer or an engineer?",
        "Is retention a better signal than NPS?",
        "what does Tony Fadell say about hardware",
    ],
)
def test_questions_route_to_qa(message: str) -> None:
    assert route(message).skill is Skill.QA


@pytest.mark.parametrize(
    "message",
    [
        "Write a Ship 30 essay about growth loops",
        "Draft an essay on why PMs should talk to users",
        "Turn this into an essay",
        "Give me a 1,250 word essay on pricing",
    ],
)
def test_content_requests_route_to_the_essay_skill(message: str) -> None:
    assert route(message).skill is Skill.SHIP30_ESSAY


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("Make an HTML one-pager on user interviews", "html"),
        ("Build a styled web page summarising this", "html"),
        ("Create a markdown checklist for launch", "markdown"),
        ("Make me a cheat sheet on growth loops", "markdown"),
        ("Give me a one-pager on hiring", "markdown"),
    ],
)
def test_artifact_requests_route_with_the_right_kind(message: str, kind: str) -> None:
    decision = route(message)
    assert decision.skill is Skill.ARTIFACT
    assert decision.artifact_kind == kind


def test_a_question_that_mentions_a_document_is_still_a_question() -> None:
    """Routing is biased toward QA on purpose.

    A wrong QA route costs a slightly plain answer. A wrong artifact route
    produces a document nobody asked for.
    """
    assert route("What should a good PRD document contain?").skill is Skill.QA
    assert route("How do you write a one-pager?").skill is Skill.QA


def test_explicit_selection_always_beats_inference() -> None:
    decision = route("How do you find PMF?", forced_skill="ship30_essay")
    assert decision.skill is Skill.SHIP30_ESSAY
    assert decision.confidence == 1.0

    decision = route("Write an essay on pricing", forced_skill="qa")
    assert decision.skill is Skill.QA


def test_unknown_forced_skill_falls_back_to_inference() -> None:
    assert route("Write an essay on pricing", forced_skill="nonsense").skill is (
        Skill.SHIP30_ESSAY
    )


def test_empty_message_is_safe() -> None:
    assert route("   ").skill is Skill.QA


# --------------------------------------------------------------------------
# Follow-up rewriting
# --------------------------------------------------------------------------


def test_pronoun_followup_inherits_the_previous_question() -> None:
    history = [
        ChatMessage(Role.USER, "How do you find product/market fit?"),
        ChatMessage(Role.ASSISTANT, "Retention is the signal that matters."),
    ]
    query = build_search_query("What about for B2B?", history)

    assert "product/market fit" in query
    assert "B2B" in query


def test_a_self_contained_question_is_not_rewritten() -> None:
    history = [ChatMessage(Role.USER, "How do you find product/market fit?")]
    message = "What is the most common mistake founders make when pricing a B2B product?"

    assert build_search_query(message, history) == message


def test_followup_with_no_history_is_returned_unchanged() -> None:
    assert build_search_query("What about that?", []) == "What about that?"


# --------------------------------------------------------------------------
# Ship 30 skill: principles are encoded, and the validator enforces them
# --------------------------------------------------------------------------


def test_principles_block_contains_the_encoded_rules() -> None:
    block = ship30.principles_block()

    # All six openers, by name, from the published source.
    for opener in ship30.OPENERS:
        assert opener.name in block
    assert "1/3/1" in block
    assert "bulleted list" in block
    assert str(ship30.TARGET_WORDS) in block


def test_sources_are_recorded_so_the_encoding_is_auditable() -> None:
    assert len(ship30.SOURCES) >= 3
    assert all(s.startswith("https://www.ship30for30.com/") for s in ship30.SOURCES)


def _well_formed_essay(words: int = 1250) -> str:
    body = " ".join(["insight"] * (words - 60))
    return (
        "# Five Things Great Growth Teams Do Differently\n\n"
        "Retention is the only signal that matters [1].\n\n"
        "## The first thing\n\n"
        f"**This is the point.** {body} [2]\n\n"
        "- One thing\n- Another thing\n- A third thing\n\n"
        "## The second thing\n\nMore substance here [1].\n\n"
        "## The takeaway\n\nDo this on Monday.\n"
    )


def test_a_well_formed_essay_passes() -> None:
    critique = ship30.critique(_well_formed_essay())
    assert critique.passed, critique.failures
    assert 1000 <= critique.word_count <= 1500


def test_short_essay_fails_with_an_actionable_message() -> None:
    critique = ship30.critique("# Title\n\nToo short [1].\n\n## A\n\n- x\n\n**bold**")
    assert not critique.passed
    assert any("Too short" in f for f in critique.failures)
    # The repair brief must name the defect, not just ask for "better".
    assert "Expand" in critique.repair_instruction()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda e: e.replace("# Five Things Great Growth Teams Do Differently\n\n", ""), "H1"),
        (lambda e: e.replace("## ", "Section: "), "section headings"),
        (lambda e: e.replace("- One thing\n- Another thing\n- A third thing\n", ""), "bulleted list"),
        (lambda e: e.replace("**This is the point.**", "This is the point."), "bold"),
        (lambda e: e.replace("[1]", "").replace("[2]", ""), "citations"),
    ],
)
def test_validator_catches_each_missing_element(mutation, expected: str) -> None:
    critique = ship30.critique(mutation(_well_formed_essay()))
    assert not critique.passed
    assert any(expected.lower() in f.lower() for f in critique.failures), critique.failures


def test_word_count_ignores_markdown_syntax() -> None:
    plain = ship30.word_count("one two three")
    marked = ship30.word_count("# one\n\n**two** *three*")
    assert plain == marked == 3


def test_first_sentence_skips_the_title() -> None:
    essay = "# A Headline Here\n\nThis is the hook. And this is not.\n"
    assert ship30.first_sentence(essay) == "This is the hook."
