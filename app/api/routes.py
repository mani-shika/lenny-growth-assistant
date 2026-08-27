"""HTTP routes.

Thin by design: validate, call into the agent or persistence layer, shape the
response. No prompt text, no retrieval logic and no provider knowledge lives
here, which is what keeps the agent layer testable without an HTTP client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import __version__
from app.agent import registry
from app.agent.orchestrator import run_turn
from app.agent.types import ChatMessage, Role
from app.api import schemas
from app.core.config import settings
from app.core.errors import ArtifactNotFound, CorpusNotIndexed, SessionNotFound
from app.core.logging import get_logger
from app.db import session as db_module
from app.db.models import Artifact, Chunk, Document, Message, Session
from app.db.session import get_db
from app.rag import embeddings as embeddings_module
from app.rag.retriever import retriever

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["api"])
DB = Annotated[AsyncSession, Depends(get_db)]


# --------------------------------------------------------------------------
# Health and configuration
# --------------------------------------------------------------------------


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Is the process up? Never touches the database or a model."""
    return {"status": "ok", "version": __version__}


@router.get("/health", response_model=schemas.HealthResponse, summary="Deep health")
async def health(db: DB) -> schemas.HealthResponse:
    """Everything an operator needs to diagnose a broken deployment in one call.

    Reports `degraded` rather than `down` whenever the system can still answer
    questions - a missing cloud key or absent embeddings are not outages.
    """
    database_ok = await db_module.ping()
    corpus = await _corpus_status(db)
    providers = await registry.health_all()
    embeddings_status = await embeddings_module.probe()

    active = settings.llm_provider
    provider_out = [
        schemas.ProviderStatusOut(**p.to_dict(), active=p.name == active)
        for p in providers
    ]

    checks: list[str] = []
    if not database_ok:
        checks.append("Postgres is unreachable - check DATABASE_URL and the db container.")
    if not corpus.indexed:
        checks.append("No corpus indexed - run `python scripts/ingest.py`.")
    if corpus.indexed and corpus.embedded_chunks == 0:
        checks.append(
            f"Lexical retrieval only - no embeddings. Run "
            f"`ollama pull {settings.embedding_model}` then re-ingest for hybrid search."
        )
    servable = [p for p in providers if p.configured and p.reachable]
    if not servable:
        checks.append("No model provider is reachable - the assistant cannot generate.")
    elif not any(p.name == active for p in servable):
        checks.append(
            f"Active provider '{active}' is not reachable; the fallback chain will be used."
        )

    if not database_ok or not servable:
        overall = "down"
    elif checks:
        overall = "degraded"
    else:
        overall = "ok"

    return schemas.HealthResponse(
        status=overall,
        version=__version__,
        database=database_ok,
        corpus=corpus,
        providers=provider_out,
        active_provider=active,
        fallback_chain=list(settings.fallback_chain),
        embeddings=embeddings_status,
        checks=checks,
    )


@router.get("/config", response_model=schemas.ConfigResponse, summary="Runtime config")
async def config() -> schemas.ConfigResponse:
    """Non-secret configuration. Powers the provider badge in the UI."""
    providers = await registry.health_all()
    return schemas.ConfigResponse(
        active_provider=settings.llm_provider,
        active_model=settings.model_for(settings.llm_provider),
        fallback_chain=list(settings.fallback_chain),
        providers=[
            schemas.ProviderStatusOut(
                **p.to_dict(), active=p.name == settings.llm_provider
            )
            for p in providers
        ],
        retrieval_top_k=settings.retrieval_top_k,
        embeddings_enabled=settings.embeddings_enabled,
    )


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


@router.post(
    "/sessions",
    response_model=schemas.SessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new chat",
)
async def create_session(
    request: Request,
    db: DB,
    payload: schemas.CreateSessionRequest = Body(default_factory=schemas.CreateSessionRequest),
) -> schemas.SessionOut:
    session = Session(
        title=payload.title or "New chat",
        user_id=payload.user_id,
        user_agent=request.headers.get("user-agent", "")[:400] or None,
        provider=settings.llm_provider,
        model=settings.model_for(settings.llm_provider),
        extra=payload.metadata,
    )
    db.add(session)
    # Commit before returning, never in dependency teardown: the client may
    # issue its next request before teardown runs. See app/db/session.py.
    await db.commit()
    log.info("session.created", session_id=session.id, user_id=session.user_id)
    return _session_out(session, message_count=0)


@router.get(
    "/sessions", response_model=list[schemas.SessionOut], summary="List chats"
)
async def list_sessions(
    db: DB,
    user_id: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[schemas.SessionOut]:
    counts = (
        select(Message.session_id, func.count().label("n"))
        .group_by(Message.session_id)
        .subquery()
    )
    stmt = (
        select(Session, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.session_id == Session.id)
        .order_by(Session.updated_at.desc())
        .limit(limit)
    )
    if user_id:
        stmt = stmt.where(Session.user_id == user_id)
    rows = (await db.execute(stmt)).all()
    return [_session_out(s, message_count=int(n)) for s, n in rows]


@router.get(
    "/sessions/{session_id}",
    response_model=schemas.SessionDetailOut,
    summary="Get a chat with its full history",
)
async def get_session(session_id: str, db: DB) -> schemas.SessionDetailOut:
    session = await _load_session(db, session_id, with_children=True)
    artifacts_by_message = {a.message_id: a for a in session.artifacts if a.message_id}
    return schemas.SessionDetailOut(
        **_session_out(session, message_count=len(session.messages)).model_dump(),
        messages=[
            _message_out(m, artifacts_by_message.get(m.id)) for m in session.messages
        ],
        artifacts=[_artifact_out(a) for a in session.artifacts],
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a chat",
)
async def delete_session(session_id: str, db: DB) -> Response:
    session = await _load_session(db, session_id)
    await db.delete(session)
    await db.commit()
    log.info("session.deleted", session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/messages",
    response_model=schemas.ChatResponse,
    summary="Send a message and get a grounded answer",
)
async def send_message(
    session_id: str, payload: schemas.ChatRequest, db: DB
) -> schemas.ChatResponse:
    """The main endpoint. Routes, retrieves, generates, persists, returns."""
    session = await _load_session(db, session_id, with_children=True)

    if not retriever.is_ready:
        # Try once before giving up - the index may simply not be loaded yet
        # in this worker (for example after a restart).
        await retriever.load(db)
    if not retriever.is_ready:
        raise CorpusNotIndexed("The transcript index is empty.")

    history = [
        ChatMessage(Role(m.role), m.content)
        for m in session.messages
        if m.role in ("user", "assistant")
    ]

    user_message = Message(session_id=session.id, role="user", content=payload.message)
    db.add(user_message)
    await db.flush()

    result = await run_turn(
        db,
        message=payload.message,
        history=history,
        forced_skill=payload.skill,
        provider_override=payload.provider,
    )

    assistant_message = Message(
        session_id=session.id,
        role="assistant",
        content=result.answer,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        usage={**result.usage, "attempts": result.attempts},
        citations=result.citations,
        route=result.route.skill.value,
        tool_calls=[],
    )
    db.add(assistant_message)
    await db.flush()

    artifact_row: Artifact | None = None
    if result.artifact is not None:
        artifact_row = Artifact(
            session_id=session.id,
            message_id=assistant_message.id,
            kind=result.artifact.kind,
            title=result.artifact.title,
            content=result.artifact.content,
            raw_content=result.artifact.raw_content,
            sanitiser_report=result.artifact.report.to_dict(),
        )
        db.add(artifact_row)
        await db.flush()

    # First real question names the chat, so the sidebar is scannable.
    if session.title == "New chat":
        session.title = payload.message[:80]
    session.updated_at = datetime.now(timezone.utc)

    # The whole turn - user message, assistant message, artifact, title - lands
    # in one transaction, committed before the response is returned.
    await db.commit()

    return schemas.ChatResponse(
        session_id=session.id,
        user_message=_message_out(user_message, None),
        assistant_message=_message_out(assistant_message, artifact_row),
        artifact=_artifact_out(artifact_row) if artifact_row else None,
        route=schemas.RouteOut(**result.route.to_dict()),
        retrieval_strategy=result.retrieval.strategy,
        retrieved_chunks=len(result.retrieval.chunks),
        provider_attempts=result.attempts,
        grounded=result.grounded,
        citations_matched=result.citations_matched,
        essay_critique=result.essay_critique,
    )


# --------------------------------------------------------------------------
# Artifacts and corpus
# --------------------------------------------------------------------------


@router.get(
    "/artifacts/{artifact_id}",
    response_model=schemas.ArtifactOut,
    summary="Fetch a rendered artifact",
)
async def get_artifact(artifact_id: str, db: DB) -> schemas.ArtifactOut:
    artifact = await db.get(Artifact, artifact_id)
    if artifact is None:
        raise ArtifactNotFound(f"No artifact with id {artifact_id}.")
    return _artifact_out(artifact)


@router.get(
    "/corpus", response_model=schemas.CorpusStatusOut, summary="Knowledge base status"
)
async def corpus_status(db: DB) -> schemas.CorpusStatusOut:
    return await _corpus_status(db)


@router.post(
    "/corpus/reindex",
    response_model=schemas.ReindexResponse,
    summary="Re-ingest the corpus",
)
async def reindex(
    db: DB,
    payload: schemas.ReindexRequest = Body(default_factory=schemas.ReindexRequest),
) -> schemas.ReindexResponse:
    """Rebuild the index.

    Synchronous on purpose: for a 60-document corpus it takes seconds, and an
    evaluator watching the response is better served by a result than by a job
    id they then have to poll.
    """
    from app.rag.ingest import ensure_corpus, ingest

    corpus_dir = ensure_corpus(refresh=payload.refresh_corpus)
    report = await ingest(db, corpus_dir=corpus_dir, force=payload.force)
    await db.commit()
    return schemas.ReindexResponse(**report.to_dict())


# --------------------------------------------------------------------------
# Mapping helpers
# --------------------------------------------------------------------------


async def _load_session(
    db: AsyncSession, session_id: str, *, with_children: bool = False
) -> Session:
    stmt = select(Session).where(Session.id == session_id)
    if with_children:
        stmt = stmt.options(
            selectinload(Session.messages), selectinload(Session.artifacts)
        )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if session is None:
        raise SessionNotFound(f"No session with id {session_id}.")
    return session


async def _corpus_status(db: AsyncSession) -> schemas.CorpusStatusOut:
    documents = (await db.execute(select(func.count()).select_from(Document))).scalar_one()
    chunks = (await db.execute(select(func.count()).select_from(Chunk))).scalar_one()
    embedded = (
        await db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.embedding.is_not(None))
        )
    ).scalar_one()
    by_type = dict(
        (
            await db.execute(
                select(Document.doc_type, func.count()).group_by(Document.doc_type)
            )
        ).all()
    )
    return schemas.CorpusStatusOut(
        documents=int(documents),
        chunks=int(chunks),
        embedded_chunks=int(embedded),
        indexed=int(chunks) > 0,
        podcasts=int(by_type.get("podcast", 0)),
        newsletters=int(by_type.get("newsletter", 0)),
    )


def _session_out(session: Session, *, message_count: int) -> schemas.SessionOut:
    return schemas.SessionOut(
        id=session.id,
        title=session.title,
        user_id=session.user_id,
        provider=session.provider,
        model=session.model,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=message_count,
    )


def _message_out(message: Message, artifact: Artifact | None) -> schemas.MessageOut:
    return schemas.MessageOut(
        id=message.id,
        session_id=message.session_id,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        created_at=message.created_at,
        provider=message.provider,
        model=message.model,
        latency_ms=message.latency_ms,
        usage=message.usage or {},
        citations=[schemas.CitationOut(**c) for c in (message.citations or [])],
        route=message.route,
        artifact_id=artifact.id if artifact else None,
    )


def _artifact_out(artifact: Artifact) -> schemas.ArtifactOut:
    return schemas.ArtifactOut(
        id=artifact.id,
        session_id=artifact.session_id,
        message_id=artifact.message_id,
        kind=artifact.kind,  # type: ignore[arg-type]
        title=artifact.title,
        content=artifact.content,
        sanitiser_report=artifact.sanitiser_report or {},
        created_at=artifact.created_at,
    )
