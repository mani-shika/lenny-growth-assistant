"""OpenAI-compatible chat completions - covers both Groq and OpenAI.

Groq serves the OpenAI wire format, so one implementation with a different base
URL, key and model covers both providers. That is why `LLM_PROVIDER=groq` and
`LLM_PROVIDER=openai` require no new code: only configuration.

Raw `httpx` rather than the `openai` package, on purpose - the surface we need
is one POST, and avoiding the dependency keeps the container small and removes
a version-pinning conflict with the Anthropic path.
"""

from __future__ import annotations

import time

import httpx

from app.agent.providers.base import BaseProvider
from app.agent.types import ChatMessage, ProviderHealth, ProviderResponse, Usage
from app.core.config import settings
from app.core.errors import (
    ProviderNotConfigured,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.core.logging import get_logger

log = get_logger(__name__)


class OpenAICompatProvider(BaseProvider):
    def __init__(self, name: str, base_url: str, api_key: str, model: str) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> ProviderResponse:
        if not self._api_key:
            raise ProviderNotConfigured(
                f"{self.name} has no API key configured.", provider=self.name
            )

        payload = {
            "model": self._model,
            "messages": [m.to_wire() for m in messages],
            "max_tokens": max_tokens or settings.llm_max_tokens,
            "temperature": (
                settings.llm_temperature if temperature is None else temperature
            ),
            "stream": False,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=timeout or settings.llm_timeout_seconds
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                f"{self.name} timed out after {timeout or settings.llm_timeout_seconds}s",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"{self.name} is unreachable: {exc}", provider=self.name
            ) from exc

        if response.status_code == 401:
            raise ProviderNotConfigured(
                f"{self.name} rejected the API key (401).", provider=self.name
            )
        if response.status_code == 429:
            # Surfaced as unavailable so the fallback chain advances rather
            # than the user seeing a rate-limit error they cannot act on.
            raise ProviderUnavailable(
                f"{self.name} rate limited this request (429).", provider=self.name
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"{self.name} returned {response.status_code}: {response.text[:300]}",
                provider=self.name,
            )

        body = response.json()
        choices = body.get("choices") or []
        text = (choices[0].get("message", {}).get("content") or "") if choices else ""
        usage_raw = body.get("usage") or {}

        return ProviderResponse(
            text=text.strip(),
            provider=self.name,
            model=body.get("model") or self._model,
            usage=Usage(
                input_tokens=int(usage_raw.get("prompt_tokens") or 0),
                output_tokens=int(usage_raw.get("completion_tokens") or 0),
            ),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def health(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                name=self.name,
                configured=False,
                reachable=False,
                model=self._model,
                detail="No API key set.",
            )
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"{self._base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            ok = response.status_code < 400
            return ProviderHealth(
                name=self.name,
                configured=True,
                reachable=ok,
                model=self._model,
                detail="" if ok else f"HTTP {response.status_code}",
            )
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(
                name=self.name,
                configured=True,
                reachable=False,
                model=self._model,
                detail=str(exc)[:200],
            )


def groq_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="groq",
        base_url=settings.groq_base_url,
        api_key=settings.groq_api_key,
        model=settings.groq_model,
    )


def openai_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="openai",
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.openai_model,
    )
