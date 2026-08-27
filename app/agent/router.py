"""Skill routing.

**Why rules and not a classifier LLM.** The demo's mandatory local model is a
2 GB llama3.2. Asking it to emit a reliable skill label costs a full extra
round trip (seconds, locally) and still misfires often enough to matter. The
routing signal we actually need is close to explicit: users say "write an
essay" or "make me an HTML one-pager". Deterministic rules over those phrasings
are faster, free, unit-testable, and - most importantly - the failure is
legible: you can read why a message routed the way it did.

**The escape hatch matters more than the rules.** The UI exposes the three
skills as buttons, and an explicit choice always wins over inference
(`forced_skill`). Users get determinism when they want it; the rules only have
to handle the conversational case.

Routing is intentionally biased toward QA. A wrong QA route produces a slightly
plain answer; a wrong essay route produces 1,250 words nobody asked for.
"""

from __future__ import annotations

import re

from app.agent.types import RouteDecision, Skill

# --- Ship 30 essay ---------------------------------------------------------
ESSAY_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\bship\s*30\b", re.I), 0.99),
    (re.compile(r"\b(write|draft|create|generate|give me)\b[^.?!]{0,40}\bessay\b", re.I), 0.95),
    (re.compile(r"\b(essay|blog post|article|newsletter post)\b[^.?!]{0,30}\babout\b", re.I), 0.85),
    (re.compile(r"\b1[,.]?250\s*words?\b", re.I), 0.9),
    (re.compile(r"\bturn (this|that|it) into (an? )?(essay|post|article)\b", re.I), 0.95),
]

# --- Artifact --------------------------------------------------------------
HTML_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\bhtml\b", re.I), 0.9),
    (re.compile(r"\bcss\b", re.I), 0.8),
    (re.compile(r"\b(landing|web)\s*page\b", re.I), 0.85),
    (re.compile(r"\b(styled|interactive|rendered)\b[^.?!]{0,30}\b(page|card|table|dashboard)\b", re.I), 0.8),
]
MARKDOWN_PATTERNS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\bmarkdown\b", re.I), 0.9),
    (re.compile(r"\b(one[- ]pager|cheat\s*sheet|checklist|playbook|template|brief|memo)\b", re.I), 0.85),
    (re.compile(r"\b(create|make|build|generate|draft)\b[^.?!]{0,30}\b(doc|document|guide)\b", re.I), 0.8),
]
ARTIFACT_HINTS = re.compile(
    r"\b(artifact|render|document|doc|page|table|checklist|one[- ]pager|cheat\s*sheet)\b",
    re.I,
)

# Questions that merely *mention* a document are still questions.
QUESTION_LEAD = re.compile(r"^\s*(what|why|how|when|who|which|where|is|are|do|does|did|can|should|would)\b", re.I)


def _best(text: str, patterns: list[tuple[re.Pattern[str], float]]) -> float:
    return max((score for pattern, score in patterns if pattern.search(text)), default=0.0)


def route(message: str, forced_skill: str | None = None) -> RouteDecision:
    """Decide which skill handles this turn."""
    if forced_skill:
        try:
            skill = Skill(forced_skill)
        except ValueError:
            pass
        else:
            kind = _artifact_kind(message) if skill is Skill.ARTIFACT else None
            return RouteDecision(
                skill=skill,
                confidence=1.0,
                reason="User selected this skill explicitly in the UI.",
                artifact_kind=kind,
            )

    text = message.strip()
    if not text:
        return RouteDecision(Skill.QA, 1.0, "Empty message; defaulting to Q&A.")

    essay_score = _best(text, ESSAY_PATTERNS)
    html_score = _best(text, HTML_PATTERNS)
    markdown_score = _best(text, MARKDOWN_PATTERNS)
    artifact_score = max(html_score, markdown_score)

    # A leading question word is strong evidence of a question, so an incidental
    # "document" or "page" must not drag the turn into artifact generation.
    if QUESTION_LEAD.match(text) and artifact_score < 0.9 and essay_score < 0.9:
        return RouteDecision(
            Skill.QA,
            0.8,
            "Phrased as a question; answering from the transcripts.",
        )

    if essay_score >= artifact_score and essay_score >= 0.85:
        return RouteDecision(
            Skill.SHIP30_ESSAY,
            essay_score,
            "Message asks for long-form written content.",
        )

    if artifact_score >= 0.8 or (artifact_score > 0 and ARTIFACT_HINTS.search(text)):
        return RouteDecision(
            Skill.ARTIFACT,
            artifact_score,
            "Message asks for a rendered document or page.",
            artifact_kind="html" if html_score >= markdown_score and html_score > 0 else "markdown",
        )

    return RouteDecision(
        Skill.QA, 0.7, "No explicit content-generation request; answering the question."
    )


def _artifact_kind(message: str) -> str:
    html_score = _best(message, HTML_PATTERNS)
    markdown_score = _best(message, MARKDOWN_PATTERNS)
    return "html" if html_score > markdown_score else "markdown"
