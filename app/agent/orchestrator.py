"""The agent loop: route -> retrieve -> generate -> verify -> persist.

Orchestration here is **deterministic**, and that is a deliberate architectural
choice rather than a shortcut. The alternative - hand every provider a tool
schema and let the model decide when to search - was tried and rejected for one
concrete reason: the demo's mandatory local model (llama3.2, 2 GB) does not
drive a tool loop reliably. It skips the search and answers from parametric
memory, which is precisely the failure mode this product exists to prevent.

So retrieval is not optional and not the model's decision. Every grounded turn
searches first, and the model only ever sees a question next to evidence. On the
Anthropic provider the same skills are additionally exposed as in-process MCP
tools, where model-driven tool selection *is* reliable - see
`providers/anthropic_agent.py`.

Trade-off, stated plainly: we lose the ability to do multi-hop retrieval that a
capable model could plan for itself. We gain a system whose grounding behaviour
is identical on a laptop and in the cloud. For an internal assistant whose whole
value is trustworthy citations, that trade is worth making.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import registry
from app.agent.router import route
from app.agent.types import ChatMessage, RouteDecision, Role, Skill
from app.core.config import settings
from app.core.logging import get_logger, timed
from app.db.models import Document
from app.rag.retriever import RetrievalResult, retriever
from app.skills import artifact as artifact_skill
from app.skills import qa as qa_skill
from app.skills import ship30

log = get_logger(__name__)

# How many prior turns to replay to the model. Small local models degrade badly
# with long histories, and the retrieval context is where the real signal is.
HISTORY_TURNS = 6
# Pronoun-led follow-ups ("what about that?") retrieve nothing on their own, so
# they get the previous question stitched on. Anything longer stands alone.
FOLLOWUP_WORD_LIMIT = 12
FOLLOWUP_MARKERS = (
    "it", "that", "this", "they", "them", "those", "these",
    "he", "she", "his", "her", "their", "same", "instead", "also", "more",
)


@dataclass(slots=True)
class TurnResult:
    """Everything a single turn produced, ready to persist and return."""

    answer: str
    route: RouteDecision
    retrieval: RetrievalResult
    provider: str
    model: str
    latency_ms: float
    usage: dict[str, int]
    attempts: list[dict[str, Any]]
    citations: list[dict[str, Any]] = field(default_factory=list)
    artifact: artifact_skill.ExtractedArtifact | None = None
    essay_critique: dict[str, Any] | None = None
    grounded: bool = True
    # False when the model produced no usable [n] markers and `citations` is
    # therefore "what we retrieved" rather than "what the answer cited". The UI
    # labels the two differently - claiming the second when we only have the
    # first is exactly the kind of false precision this product must not ship.
    citations_matched: bool = True


def build_search_query(message: str, history: list[ChatMessage]) -> str:
    """Expand a follow-up into something worth searching for.

    "What about B2B?" retrieves noise. "How do you find product/market fit?
    What about B2B?" retrieves the right passages. This is cheap query rewriting
    with no extra model call - which matters when the model is running locally.
    """
    text = message.strip()
    words = text.split()
    if len(words) > FOLLOWUP_WORD_LIMIT:
        return text

    lowered = {w.strip(".,?!").lower() for w in words}
    looks_like_followup = bool(lowered & set(FOLLOWUP_MARKERS)) or len(words) <= 5
    if not looks_like_followup:
        return text

    previous = next(
        (m.content for m in reversed(history) if m.role is Role.USER),
        "",
    )
    return f"{previous} {text}".strip() if previous else text


async def run_turn(
    session: AsyncSession,
    *,
    message: str,
    history: list[ChatMessage],
    forced_skill: str | None = None,
    provider_override: str | None = None,
) -> TurnResult:
    """Execute one user turn end to end."""
    started = time.perf_counter()
    decision = route(message, forced_skill)

    search_query = build_search_query(message, history)
    with timed(log, "turn.retrieve", skill=decision.skill.value) as fields:
        retrieval = await retriever.search(session, search_query)
        fields["chunks"] = len(retrieval.chunks)
        fields["strategy"] = retrieval.strategy

    if retrieval.is_empty:
        return await _no_evidence_turn(session, decision, retrieval, started)

    context = retrieval.as_context()

    kwargs = {
        "message": message,
        "context": context,
        "history": history,
        "decision": decision,
        "retrieval": retrieval,
        "provider_override": provider_override,
    }
    if decision.skill is Skill.SHIP30_ESSAY:
        result = await _run_essay(**kwargs)
    elif decision.skill is Skill.ARTIFACT:
        result = await _run_artifact(**kwargs)
    else:
        result = await _run_qa(**kwargs)

    result.citations, result.citations_matched = _cited_sources(
        result.answer, retrieval
    )
    result.latency_ms = round((time.perf_counter() - started) * 1000, 2)
    log.info(
        "turn.complete",
        skill=decision.skill.value,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        citations=len(result.citations),
        retrieval_strategy=retrieval.strategy,
        fallback_used=len(result.attempts) > 1,
    )
    return result


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------


async def _run_qa(
    *,
    message: str,
    context: str,
    history: list[ChatMessage],
    decision: RouteDecision,
    retrieval: RetrievalResult,
    provider_override: str | None = None,
) -> TurnResult:
    messages = (
        [ChatMessage(Role.SYSTEM, qa_skill.SYSTEM_PROMPT)]
        + _recent(history)
        + [ChatMessage(Role.USER, qa_skill.build_user_prompt(message, context))]
    )
    response, attempts = await registry.generate(
        messages, provider_override=provider_override
    )
    return _base_result(response, attempts, decision, retrieval)


async def _run_essay(
    *,
    message: str,
    context: str,
    history: list[ChatMessage],
    decision: RouteDecision,
    retrieval: RetrievalResult,
    provider_override: str | None = None,
) -> TurnResult:
    user_prompt = (
        f"EXCERPTS FROM THE KNOWLEDGE BASE\n{context}\n\nEND OF EXCERPTS\n\n"
        f"Brief: {message}\n\n"
        "Write the essay now, grounded entirely in the excerpts above."
    )
    messages = [
        ChatMessage(Role.SYSTEM, ship30.system_prompt()),
        ChatMessage(Role.USER, user_prompt),
    ]
    response, attempts = await registry.generate(
        messages,
        max_tokens=max(settings.llm_max_tokens, 4096),
        provider_override=provider_override,
    )

    critique = ship30.critique(response.text)
    # One repair pass, and only one. A second costs another full generation for
    # diminishing returns; the critique is returned to the UI either way, so a
    # still-imperfect essay is visibly imperfect rather than silently so.
    if not critique.passed:
        log.info(
            "ship30.repair",
            failures=critique.failures,
            word_count=critique.word_count,
        )
        repair = [
            ChatMessage(Role.SYSTEM, ship30.system_prompt()),
            ChatMessage(Role.USER, user_prompt),
            ChatMessage(Role.ASSISTANT, response.text),
            ChatMessage(Role.USER, critique.repair_instruction()),
        ]
        try:
            revised, repair_attempts = await registry.generate(
                repair,
                max_tokens=max(settings.llm_max_tokens, 4096),
                provider_override=provider_override,
            )
        except Exception as exc:  # noqa: BLE001 - keep the usable first draft
            log.warning("ship30.repair_failed", error=str(exc)[:300])
        else:
            revised_critique = ship30.critique(revised.text)
            # Only accept the revision if it is actually better.
            if len(revised_critique.failures) < len(critique.failures):
                response, critique = revised, revised_critique
                attempts += repair_attempts

    result = _base_result(response, attempts, decision, retrieval)
    result.essay_critique = critique.to_dict()
    return result


async def _run_artifact(
    *,
    message: str,
    context: str,
    history: list[ChatMessage],
    decision: RouteDecision,
    retrieval: RetrievalResult,
    provider_override: str | None = None,
) -> TurnResult:
    kind = decision.artifact_kind or "markdown"
    conversation = "\n".join(
        f"{m.role.value}: {m.content}" for m in _recent(history)
    )
    user_prompt = (
        f"EXCERPTS FROM THE KNOWLEDGE BASE\n{context}\n\nEND OF EXCERPTS\n\n"
        + (f"CONVERSATION SO FAR\n{conversation}\n\n" if conversation else "")
        + f"Request: {message}"
    )
    messages = [
        ChatMessage(Role.SYSTEM, artifact_skill.system_prompt(kind)),
        ChatMessage(Role.USER, user_prompt),
    ]
    response, attempts = await registry.generate(
        messages, provider_override=provider_override
    )

    extracted = artifact_skill.extract(response.text, preferred_kind=kind)

    # Small local models routinely ignore "emit one fenced block" on the first
    # try, which turns an HTML request into Markdown prose. One strict retry
    # recovers most of those. Only one: a second costs more than it returns,
    # and the Markdown fallback is still a usable artifact.
    if not extracted.format_honoured or (kind == "html" and extracted.kind != "html"):
        log.info(
            "artifact.format_retry",
            requested=kind,
            got=extracted.kind,
            fenced=extracted.format_honoured,
        )
        strict = [
            ChatMessage(Role.SYSTEM, artifact_skill.system_prompt(kind)),
            ChatMessage(Role.USER, user_prompt),
            ChatMessage(Role.ASSISTANT, response.text[:2000]),
            ChatMessage(
                Role.USER,
                "That reply was not in the required format. Send it again as "
                f"a TITLE: line followed by one ```{kind} fenced block, and "
                "nothing else. No introduction, no explanation.",
            ),
        ]
        try:
            retried, retry_attempts = await registry.generate(
                strict, provider_override=provider_override
            )
        except Exception as exc:  # noqa: BLE001 - keep the usable first draft
            log.warning("artifact.format_retry_failed", error=str(exc)[:300])
        else:
            candidate = artifact_skill.extract(retried.text, preferred_kind=kind)
            # Accept only a genuine improvement.
            if candidate.format_honoured and (
                kind != "html" or candidate.kind == "html"
            ):
                extracted, response = candidate, retried
                attempts += retry_attempts

    result = _base_result(response, attempts, decision, retrieval)
    result.artifact = extracted
    # The chat pane shows the model's prose; the artifact pane shows the document.
    result.answer = extracted.commentary or (
        f"I've put **{extracted.title}** in the artifact panel."
    )
    if extracted.report.modified:
        result.answer += (
            f"\n\n> **Sanitiser:** {extracted.report.summary()} "
            "The artifact renders in a sandboxed frame with scripts disabled."
        )
    log.info(
        "artifact.created",
        kind=extracted.kind,
        title=extracted.title[:80],
        sanitised=extracted.report.modified,
        removed_tags=extracted.report.removed_tags,
    )
    return result


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _base_result(
    response: Any,
    attempts: list[dict[str, Any]],
    decision: RouteDecision,
    retrieval: RetrievalResult,
) -> TurnResult:
    return TurnResult(
        answer=response.text,
        route=decision,
        retrieval=retrieval,
        provider=response.provider,
        model=response.model,
        latency_ms=response.latency_ms,
        usage=response.usage.to_dict(),
        attempts=attempts,
    )


def _recent(history: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in history if m.role is not Role.SYSTEM][-HISTORY_TURNS:]


def _cited_sources(
    answer: str, retrieval: RetrievalResult
) -> tuple[list[dict[str, Any]], bool]:
    """Return the sources the answer cited, plus whether it really cited them.

    Two reasons this is not simply "everything we retrieved": showing eight
    sources for a two-source answer trains users to ignore citations, and a
    marker pointing at an excerpt that was never returned is a bug we want to
    see rather than silently render.

    Small local models sometimes emit no usable markers at all. When that
    happens we still show provenance - the retrieved passages are genuinely
    what the answer was written from - but we flag it, so the UI can say
    "sources consulted" instead of implying a citation that was never made.
    """
    order: list[int] = []
    for raw in re.findall(r"\[(\d{1,2})\]", answer):
        index = int(raw)
        if 1 <= index <= len(retrieval.chunks) and index not in order:
            order.append(index)

    if not order:
        log.warning(
            "citations.unmatched",
            hint="model emitted no resolvable [n] markers; showing retrieved sources",
            answer_chars=len(answer),
        )
        return [c.citation.to_dict() for c in retrieval.chunks[:3]], False

    cited: list[dict[str, Any]] = []
    for index in order:
        payload = retrieval.chunks[index - 1].citation.to_dict()
        payload["marker"] = index
        cited.append(payload)
    return cited, True


async def _no_evidence_turn(
    session: AsyncSession,
    decision: RouteDecision,
    retrieval: RetrievalResult,
    started: float,
) -> TurnResult:
    """Refuse honestly, without spending a model call to do it."""
    doc_count = (
        await session.execute(select(func.count()).select_from(Document))
    ).scalar_one()
    log.info("turn.no_evidence", skill=decision.skill.value, documents=doc_count)
    return TurnResult(
        answer=qa_skill.NO_EVIDENCE_TEMPLATE.format(doc_count=doc_count),
        route=decision,
        retrieval=retrieval,
        provider="none",
        model="none",
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        attempts=[],
        grounded=False,
    )
