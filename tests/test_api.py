"""API contracts and persistence.

The model is stubbed. That is deliberate: these tests must assert that the API
keeps its promises - session isolation, citation persistence, structured errors,
validation - and a real model would make them slow, non-deterministic, and
dependent on Ollama being installed on whoever runs CI.

Provider behaviour has its own tests in test_providers.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.types import ProviderResponse, Usage
from app.db.models import Chunk, Document
from app.rag.retriever import retriever


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> AsyncSession:
    """A tiny indexed corpus, so retrieval has something real to return."""
    document = Document(
        source_path="podcasts/jane-doe.md",
        title="How to find product/market fit | Jane Doe",
        doc_type="podcast",
        guest="Jane Doe",
        published_at="2026-03-14",
        source_url="https://www.lennysnewsletter.com/p/how-to-find-pmf",
        word_count=120,
        checksum="abc123",
    )
    db_session.add(document)
    await db_session.flush()

    db_session.add_all(
        [
            Chunk(
                document_id=document.id,
                ordinal=0,
                text=(
                    "Jane Doe (00:00:20): Retention is the only signal that matters "
                    "for product market fit. If people come back on their own you "
                    "have it."
                ),
                speakers="Jane Doe",
                start_timestamp="00:00:20",
                token_estimate=40,
            ),
            Chunk(
                document_id=document.id,
                ordinal=1,
                text=(
                    "Jane Doe (00:04:41): Charge earlier than feels comfortable. "
                    "Willingness to pay is the fastest proxy for value."
                ),
                speakers="Jane Doe",
                start_timestamp="00:04:41",
                token_estimate=30,
            ),
        ]
    )
    await db_session.commit()
    await retriever.load(db_session)
    return db_session


@pytest_asyncio.fixture
async def client(seeded: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    from app.db.session import get_db
    from app.main import app

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield seeded

    async def fake_generate(messages, **_kwargs):  # noqa: ANN001, ANN202
        # Echo a citation so the citation-resolution path is exercised.
        return (
            ProviderResponse(
                text="Retention is the signal that matters [1].",
                provider="stub",
                model="stub-model",
                usage=Usage(input_tokens=100, output_tokens=20),
                latency_ms=12.5,
            ),
            [{"provider": "stub", "outcome": "ok"}],
        )

    monkeypatch.setattr("app.agent.orchestrator.registry.generate", fake_generate)
    app.dependency_overrides[get_db] = override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


async def test_liveness_needs_no_dependencies(client: AsyncClient) -> None:
    response = await client.get("/api/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_reports_corpus_and_providers(client: AsyncClient) -> None:
    body = (await client.get("/api/health")).json()

    assert body["corpus"]["documents"] == 1
    assert body["corpus"]["chunks"] == 2
    assert body["corpus"]["indexed"] is True
    assert {p["name"] for p in body["providers"]} == {
        "ollama",
        "groq",
        "openai",
        "anthropic",
    }
    assert body["status"] in {"ok", "degraded", "down"}


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/api/health/live")
    assert response.headers.get("x-request-id")


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


async def test_create_and_fetch_a_session(client: AsyncClient) -> None:
    created = await client.post("/api/sessions", json={"title": "My chat"})
    assert created.status_code == 201
    session_id = created.json()["id"]

    fetched = await client.get(f"/api/sessions/{session_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "My chat"
    assert fetched.json()["messages"] == []


async def test_missing_session_returns_a_structured_404(client: AsyncClient) -> None:
    response = await client.get("/api/sessions/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "session_not_found"
    assert error["hint"]  # an operator-facing next step, not just a message
    assert error["request_id"]


async def test_sessions_keep_independent_context(client: AsyncClient) -> None:
    """The core session guarantee: one chat never sees another's history."""
    first = (await client.post("/api/sessions", json={})).json()["id"]
    second = (await client.post("/api/sessions", json={})).json()["id"]

    await client.post(
        f"/api/sessions/{first}/messages", json={"message": "How do I find PMF?"}
    )

    first_detail = (await client.get(f"/api/sessions/{first}")).json()
    second_detail = (await client.get(f"/api/sessions/{second}")).json()

    assert len(first_detail["messages"]) == 2
    assert second_detail["messages"] == []


async def test_deleting_a_session_cascades_to_its_messages(client: AsyncClient) -> None:
    session_id = (await client.post("/api/sessions", json={})).json()["id"]
    await client.post(
        f"/api/sessions/{session_id}/messages", json={"message": "How do I find PMF?"}
    )

    assert (await client.delete(f"/api/sessions/{session_id}")).status_code == 204
    assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404


async def test_session_is_titled_from_the_first_message(client: AsyncClient) -> None:
    session_id = (await client.post("/api/sessions", json={})).json()["id"]
    await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"message": "How do you find product market fit?"},
    )

    listed = (await client.get("/api/sessions")).json()
    assert listed[0]["title"].startswith("How do you find")


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


async def test_a_turn_is_persisted_with_its_provenance(client: AsyncClient) -> None:
    session_id = (await client.post("/api/sessions", json={})).json()["id"]

    response = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"message": "How do you know when you have product market fit?"},
    )
    assert response.status_code == 200
    body = response.json()

    assistant = body["assistant_message"]
    assert assistant["role"] == "assistant"
    assert assistant["provider"] == "stub"
    assert assistant["model"] == "stub-model"
    assert assistant["usage"]["total_tokens"] == 120
    assert body["retrieved_chunks"] > 0

    # And it survives a round trip through the database.
    persisted = (await client.get(f"/api/sessions/{session_id}")).json()["messages"]
    assert [m["role"] for m in persisted] == ["user", "assistant"]
    assert persisted[1]["citations"], "citations must be persisted, not just returned"


async def test_citation_markers_resolve_to_real_sources(client: AsyncClient) -> None:
    session_id = (await client.post("/api/sessions", json={})).json()["id"]

    body = (
        await client.post(
            f"/api/sessions/{session_id}/messages",
            json={"message": "How do you know when you have product market fit?"},
        )
    ).json()

    assert body["citations_matched"] is True
    citation = body["assistant_message"]["citations"][0]
    assert citation["marker"] == 1
    assert citation["document_title"].startswith("How to find product/market fit")
    assert citation["timestamp"] == "00:00:20"
    # The deep link must carry the moment, not just the episode.
    assert citation["source_url"].endswith("#t=20")


async def test_out_of_corpus_question_is_refused_without_calling_a_model(
    client: AsyncClient,
) -> None:
    """The honesty guarantee, end to end."""
    session_id = (await client.post("/api/sessions", json={})).json()["id"]

    body = (
        await client.post(
            f"/api/sessions/{session_id}/messages",
            json={"message": "What is the treatment protocol for acute pancreatitis?"},
        )
    ).json()

    assert body["grounded"] is False
    assert body["retrieved_chunks"] == 0
    assert body["retrieval_strategy"] == "empty"
    assert body["assistant_message"]["provider"] == "none"
    assert "could not find" in body["assistant_message"]["content"].lower()


async def test_forced_skill_overrides_the_router(client: AsyncClient) -> None:
    session_id = (await client.post("/api/sessions", json={})).json()["id"]

    body = (
        await client.post(
            f"/api/sessions/{session_id}/messages",
            json={"message": "How do I find product market fit?", "skill": "artifact"},
        )
    ).json()

    assert body["route"]["skill"] == "artifact"
    assert body["route"]["confidence"] == 1.0


@pytest.mark.parametrize(
    "payload",
    [
        {"message": ""},
        {"message": "   "},
        {},
        {"message": "hi", "skill": "not_a_skill"},
        {"message": "hi", "provider": "not_a_provider"},
    ],
)
async def test_invalid_requests_return_422_not_500(
    client: AsyncClient, payload: dict
) -> None:
    session_id = (await client.post("/api/sessions", json={})).json()["id"]
    response = await client.post(f"/api/sessions/{session_id}/messages", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


async def test_message_to_a_missing_session_is_a_404(client: AsyncClient) -> None:
    response = await client.post(
        "/api/sessions/nope/messages", json={"message": "hello"}
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


async def test_artifact_is_persisted_and_retrievable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def html_generate(messages, **_kwargs):  # noqa: ANN001, ANN202
        return (
            ProviderResponse(
                text=(
                    "TITLE: PMF Signals\n```html\n<h1>PMF Signals</h1>"
                    "<script>alert(1)</script><p>Retention [1].</p>\n```"
                ),
                provider="stub",
                model="stub-model",
                usage=Usage(),
            ),
            [{"provider": "stub", "outcome": "ok"}],
        )

    monkeypatch.setattr("app.agent.orchestrator.registry.generate", html_generate)
    session_id = (await client.post("/api/sessions", json={})).json()["id"]

    body = (
        await client.post(
            f"/api/sessions/{session_id}/messages",
            # Phrased with words the seeded corpus actually contains: the
            # coverage gate is relative to corpus size, and a two-chunk fixture
            # legitimately does not cover "HTML one-pager".
            json={"message": "retention and product market fit", "skill": "artifact"},
        )
    ).json()

    artifact = body["artifact"]
    assert artifact is not None
    assert artifact["kind"] == "html"
    assert artifact["title"] == "PMF Signals"
    assert "<script" not in artifact["content"].lower()
    assert artifact["sanitiser_report"]["modified"] is True

    fetched = await client.get(f"/api/artifacts/{artifact['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["content"] == artifact["content"]


async def test_missing_artifact_returns_a_structured_404(client: AsyncClient) -> None:
    response = await client.get("/api/artifacts/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "artifact_not_found"
