"""SQLAlchemy models - the persistence contract.

Five tables in two groups:

* Conversation state (``sessions``, ``messages``, ``artifacts``) - what the
  user did.
* Knowledge base (``documents``, ``chunks``) - what the assistant may ground on.

They are deliberately separate: re-ingesting the corpus never touches
conversation history, and wiping conversations never forces a re-index.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import settings


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Session(Base):
    """One conversation. Context is scoped strictly to a single session id."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), default="New chat")
    # User metadata. No auth is in scope, so this is a caller-supplied label
    # used for attribution and for scoping list queries - see PRD "Out of scope".
    user_id: Mapped[str] = mapped_column(String(120), default="anonymous")
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    # Provider/model in force when the session was created. Individual messages
    # record what actually served them, which can differ after a fallback.
    provider: Mapped[str] = mapped_column(String(40), default=settings.llm_provider)
    model: Mapped[str] = mapped_column(String(120), default="")
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_sessions_user_updated", "user_id", "updated_at"),)


class Message(Base):
    """A single turn. ``citations`` is the audit trail for a grounded answer."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)

    # Observability: which provider actually served this turn, how long it took,
    # and what it cost. Populated for assistant turns only.
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Grounding: the chunks used, with enough detail to re-render a citation
    # without re-querying the index.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Which skill handled the turn (qa | ship30_essay | artifact) plus tool calls.
    route: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[Session] = relationship(back_populates="messages")
    artifact: Mapped[Artifact | None] = relationship(
        back_populates="message", uselist=False
    )

    __table_args__ = (Index("ix_messages_session_created", "session_id", "created_at"),)


class Artifact(Base):
    """A rendered document produced by a turn.

    ``content`` is the sanitised payload the viewer renders. ``raw_content``
    keeps what the model actually produced, so a sanitiser decision can be
    audited after the fact without re-running the model.
    """

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16))  # markdown | html
    title: Mapped[str] = mapped_column(String(300), default="Untitled artifact")
    content: Mapped[str] = mapped_column(Text)
    raw_content: Mapped[str] = mapped_column(Text, default="")
    # What the sanitiser stripped, and why. Surfaced in the viewer's
    # "what was blocked" panel.
    sanitiser_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped[Session] = relationship(back_populates="artifacts")
    message: Mapped[Message | None] = relationship(back_populates="artifact")


class Document(Base):
    """One source file from the corpus. ``checksum`` drives incremental refresh."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Path relative to the corpus root, e.g. "podcasts/adam-mosseri.md".
    source_path: Mapped[str] = mapped_column(String(400), unique=True)
    title: Mapped[str] = mapped_column(String(400))
    doc_type: Mapped[str] = mapped_column(String(20))  # podcast | newsletter
    guest: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(600), nullable=True)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    # sha256 of the file body. An unchanged checksum means the document is
    # skipped on refresh, which keeps re-ingestion cheap.
    checksum: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    """A retrievable passage.

    ``start_timestamp`` is what makes a citation land on the exact moment in the
    episode rather than on the episode as a whole.
    """

    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Speakers appearing in this chunk, comma separated - used to label quotes.
    speakers: Mapped[str] = mapped_column(String(400), default="")
    start_timestamp: Mapped[str | None] = mapped_column(String(12), nullable=True)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    # Nullable: the index is fully usable lexically before embeddings exist,
    # which is what lets ingestion succeed when Ollama is unreachable.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dim), nullable=True
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunk_document_ordinal"),
    )
