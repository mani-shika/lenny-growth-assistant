"""Dense embeddings via Ollama.

Embeddings are an *optimisation*, not a dependency. Every function here returns
an empty result rather than raising when Ollama is unreachable or the model is
not pulled, and the caller is expected to carry on with lexical retrieval. That
is the single most important resilience property of the retrieval stack: a
laptop with no `nomic-embed-text` pulled still gets grounded, cited answers.
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# Ollama exposes /api/embed (batch, current) and /api/embeddings (single,
# legacy). We try the modern one first and fall back, so the app works across
# the versions an evaluator might already have installed.
_EMBED_PATH = "/api/embed"
_LEGACY_EMBED_PATH = "/api/embeddings"


class EmbeddingUnavailable(RuntimeError):
    """Raised internally; never escapes this module's public helpers."""


async def embed_texts(texts: list[str], *, timeout: float = 120.0) -> list[list[float]]:
    """Embed a batch. Returns ``[]`` if embeddings are unavailable."""
    if not settings.embeddings_enabled or not texts:
        return []

    url = settings.ollama_base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}{_EMBED_PATH}",
                json={"model": settings.embedding_model, "input": texts},
            )
            if response.status_code == 404:
                return await _legacy_embed(client, url, texts)
            response.raise_for_status()
            vectors = response.json().get("embeddings") or []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning(
            "embeddings.unavailable",
            error=str(exc)[:300],
            model=settings.embedding_model,
            impact="falling back to lexical-only retrieval",
        )
        return []

    return _validate(vectors, expected=len(texts))


async def embed_query(text: str, *, timeout: float = 30.0) -> list[float] | None:
    """Embed a single query string. ``None`` means "search lexically only"."""
    vectors = await embed_texts([text], timeout=timeout)
    return vectors[0] if vectors else None


async def _legacy_embed(
    client: httpx.AsyncClient, url: str, texts: list[str]
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        response = await client.post(
            f"{url}{_LEGACY_EMBED_PATH}",
            json={"model": settings.embedding_model, "prompt": text},
        )
        response.raise_for_status()
        vectors.append(response.json().get("embedding") or [])
    return vectors


def _validate(vectors: list[list[float]], *, expected: int) -> list[list[float]]:
    """Reject a batch whose shape does not match the configured dimension.

    A silent dimension mismatch would surface much later as a Postgres error
    on insert; catching it here keeps the failure legible.
    """
    if len(vectors) != expected:
        log.warning("embeddings.count_mismatch", got=len(vectors), expected=expected)
        return []
    if vectors and len(vectors[0]) != settings.embedding_dim:
        log.warning(
            "embeddings.dim_mismatch",
            got=len(vectors[0]),
            expected=settings.embedding_dim,
            hint="set EMBEDDING_DIM to match EMBEDDING_MODEL, then re-ingest",
        )
        return []
    return vectors


async def probe() -> dict[str, object]:
    """Health-check helper: is the embedding model actually usable right now?"""
    if not settings.embeddings_enabled:
        return {"enabled": False, "available": False, "reason": "disabled by config"}
    vector = await embed_query("health check", timeout=10.0)
    return {
        "enabled": True,
        "available": vector is not None,
        "model": settings.embedding_model,
        "dim": len(vector) if vector else None,
    }
