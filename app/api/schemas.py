"""Request and response contracts.

These are the API's promise to its clients. Every field is typed and validated
at the boundary, so a malformed request fails with a 422 naming the field rather
than a 500 from somewhere deep in the agent layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SkillName = Literal["qa", "ship30_essay", "artifact"]
ProviderNameLiteral = Literal["ollama", "groq", "openai", "anthropic"]


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    user_id: str = Field(default="anonymous", max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # Force a skill instead of letting the router infer one. The UI's skill
    # buttons set this, which is why routing never has to be perfect.
    skill: SkillName | None = None
    # Per-request provider override, for side-by-side comparison in the UI.
    # Ignored if the provider is not configured.
    provider: ProviderNameLiteral | None = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("message must contain non-whitespace text")
        return cleaned


class ReindexRequest(BaseModel):
    # Re-chunk and re-embed everything, ignoring checksums.
    force: bool = False
    # git pull the corpus before indexing.
    refresh_corpus: bool = False


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


class CitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: str
    document_title: str
    doc_type: str
    guest: str | None = None
    published_at: str | None = None
    source_url: str | None = None
    speakers: str = ""
    timestamp: str | None = None
    score: float = 0.0
    excerpt: str = ""
    marker: int | None = None


class ArtifactOut(BaseModel):
    id: str
    session_id: str
    message_id: str | None = None
    kind: Literal["markdown", "html"]
    title: str
    content: str
    sanitiser_report: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RouteOut(BaseModel):
    skill: SkillName
    confidence: float
    reason: str
    artifact_kind: str | None = None


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime
    provider: str | None = None
    model: str | None = None
    latency_ms: float | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    citations: list[CitationOut] = Field(default_factory=list)
    route: str | None = None
    artifact_id: str | None = None


class ChatResponse(BaseModel):
    """The full result of one turn - everything the UI needs, in one payload."""

    session_id: str
    user_message: MessageOut
    assistant_message: MessageOut
    artifact: ArtifactOut | None = None
    route: RouteOut
    # Diagnostics the UI surfaces in its "how was this answered?" drawer.
    retrieval_strategy: str
    retrieved_chunks: int
    provider_attempts: list[dict[str, Any]] = Field(default_factory=list)
    grounded: bool = True
    # False => `citations` are the passages retrieved, not markers the answer
    # actually used. The UI must not present them as inline citations.
    citations_matched: bool = True
    essay_critique: dict[str, Any] | None = None


class SessionOut(BaseModel):
    id: str
    title: str
    user_id: str
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class SessionDetailOut(SessionOut):
    messages: list[MessageOut] = Field(default_factory=list)
    artifacts: list[ArtifactOut] = Field(default_factory=list)


class ProviderStatusOut(BaseModel):
    name: str
    configured: bool
    reachable: bool
    model: str
    detail: str = ""
    active: bool = False


class CorpusStatusOut(BaseModel):
    documents: int
    chunks: int
    embedded_chunks: int
    indexed: bool
    podcasts: int
    newsletters: int


class HealthResponse(BaseModel):
    """Deep health. `status` is the one field a monitor needs."""

    status: Literal["ok", "degraded", "down"]
    version: str
    database: bool
    corpus: CorpusStatusOut
    providers: list[ProviderStatusOut]
    active_provider: str
    fallback_chain: list[str]
    embeddings: dict[str, Any]
    checks: list[str] = Field(default_factory=list)


class ConfigResponse(BaseModel):
    """Non-secret runtime configuration, for the UI's provider badge."""

    active_provider: str
    active_model: str
    fallback_chain: list[str]
    providers: list[ProviderStatusOut]
    retrieval_top_k: int
    embeddings_enabled: bool


class ReindexResponse(BaseModel):
    documents_seen: int
    documents_indexed: int
    documents_skipped: int
    chunks_written: int
    chunks_embedded: int
    embeddings_available: bool
    errors: list[str] = Field(default_factory=list)


class ErrorPayload(BaseModel):
    code: str
    message: str
    hint: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorPayload
