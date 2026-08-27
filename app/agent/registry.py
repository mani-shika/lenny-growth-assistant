"""Provider registry and the fallback chain.

The model toggle lives here. `LLM_PROVIDER` selects the active provider and
`LLM_FALLBACK_CHAIN` lists who to try next; both are read at call time, not at
import time, so a provider switch needs a restart at most - never an edit.

Fallback policy, and its deliberate limit: we advance the chain on
*infrastructure* failures (unreachable, timed out, rate limited, missing or
rejected key). We do **not** advance on a successful call whose answer we
dislike. Retrying a good connection because the content disappointed us would
double cost and latency for no measurable gain, and would hide quality problems
that belong in the eval loop instead.
"""

from __future__ import annotations

from typing import Any, Callable

from app.agent.providers.anthropic_agent import anthropic_provider
from app.agent.providers.base import BaseProvider
from app.agent.providers.ollama_provider import ollama_provider
from app.agent.providers.openai_compat import groq_provider, openai_provider
from app.agent.types import ChatMessage, ProviderHealth, ProviderResponse
from app.core.config import ProviderName, settings
from app.core.errors import (
    AllProvidersFailed,
    AppError,
    ProviderNotConfigured,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.core.logging import get_logger

log = get_logger(__name__)

_FACTORIES: dict[str, Callable[[], BaseProvider]] = {
    "ollama": ollama_provider,
    "groq": groq_provider,
    "openai": openai_provider,
    "anthropic": anthropic_provider,
}

# Failures that mean "this provider cannot serve me", as opposed to a bug.
RETRYABLE = (ProviderUnavailable, ProviderTimeout, ProviderNotConfigured)


def get_provider(name: str | None = None) -> BaseProvider:
    resolved = name or settings.llm_provider
    factory = _FACTORIES.get(resolved)
    if factory is None:
        raise ProviderNotConfigured(f"Unknown provider '{resolved}'.", provider=resolved)
    # Constructed per call: cheap objects, and it means a settings change is
    # picked up without clearing a cache.
    return factory()


def active_chain() -> list[ProviderName]:
    return settings.fallback_chain


async def generate(
    messages: list[ChatMessage],
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
    provider_override: str | None = None,
) -> tuple[ProviderResponse, list[dict[str, Any]]]:
    """Run the fallback chain and return the first success.

    Returns the response plus an `attempts` trail. The trail is persisted on
    the message row, so "why did this answer come from Groq when the UI said
    Ollama?" is answerable after the fact without reproducing the failure.
    """
    chain: list[str] = (
        [provider_override] if provider_override else list(active_chain())
    )
    attempts: list[dict[str, Any]] = []

    for name in chain:
        try:
            provider = get_provider(name)
        except AppError as exc:
            attempts.append({"provider": name, "outcome": "error", "error": exc.message})
            continue

        try:
            response = await provider.complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
            )
        except RETRYABLE as exc:
            log.warning(
                "provider.failed",
                provider=name,
                model=provider.model,
                error_code=exc.code,
                error=exc.message[:300],
            )
            attempts.append(
                {
                    "provider": name,
                    "model": provider.model,
                    "outcome": "failed",
                    "code": exc.code,
                    "error": exc.message[:300],
                }
            )
            continue

        if not response.text.strip():
            # An empty body from a healthy connection is a real failure mode on
            # small local models; treat it as one so the chain can recover.
            log.warning("provider.empty_response", provider=name, model=provider.model)
            attempts.append(
                {
                    "provider": name,
                    "model": provider.model,
                    "outcome": "empty",
                    "error": "Provider returned an empty completion.",
                }
            )
            continue

        attempts.append(
            {
                "provider": name,
                "model": response.model,
                "outcome": "ok",
                "latency_ms": response.latency_ms,
            }
        )
        if len(attempts) > 1:
            log.info("provider.fallback_used", served_by=name, attempts=len(attempts))
        return response, attempts

    raise AllProvidersFailed(
        "No configured model provider could serve this request.", attempts=attempts
    )


async def health_all() -> list[ProviderHealth]:
    """Probe every known provider. Used by /api/health and the UI badge."""
    results: list[ProviderHealth] = []
    for name in _FACTORIES:
        try:
            results.append(await get_provider(name).health())
        except Exception as exc:  # noqa: BLE001 - health must never raise
            results.append(
                ProviderHealth(
                    name=name,
                    configured=settings.is_configured(name),  # type: ignore[arg-type]
                    reachable=False,
                    model=settings.model_for(name),  # type: ignore[arg-type]
                    detail=str(exc)[:200],
                )
            )
    return results
