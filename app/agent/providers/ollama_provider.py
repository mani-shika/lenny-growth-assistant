"""Ollama - the local provider, and the one the submitted demo runs on.

Uses `/api/chat` (non-streaming) rather than the OpenAI-compatible shim at
`/v1`, because the native endpoint reports `prompt_eval_count` /`eval_count`,
which is what lets the UI show real token usage for a local model.

Local-model realities this class handles explicitly:

* **First call after a cold start is slow** - the model has to be loaded into
  memory. `keep_alive` holds it there so the second question is fast.
* **A missing model is the most common setup failure.** A 404 from Ollama is
  turned into a message naming the exact `ollama pull` command to run.
"""

from __future__ import annotations

import time

import httpx

from app.agent.providers.base import BaseProvider
from app.agent.types import ChatMessage, ProviderHealth, ProviderResponse, Usage
from app.core.config import settings
from app.core.errors import ProviderTimeout, ProviderUnavailable
from app.core.logging import get_logger

log = get_logger(__name__)

# Keep the model resident between turns; a reload costs several seconds.
KEEP_ALIVE = "10m"


class OllamaProvider(BaseProvider):
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self._base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self._model = model or settings.ollama_model

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
        payload = {
            "model": self._model,
            "messages": [m.to_wire() for m in messages],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {
                "temperature": (
                    settings.llm_temperature if temperature is None else temperature
                ),
                "num_predict": max_tokens or settings.llm_max_tokens,
            },
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=timeout or settings.llm_timeout_seconds
            ) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(
                "Ollama did not respond in time. A larger model on modest hardware "
                "may need a higher LLM_TIMEOUT_SECONDS.",
                provider=self.name,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"Cannot reach Ollama at {self._base_url}. Is `ollama serve` running? ({exc})",
                provider=self.name,
            ) from exc

        if response.status_code == 404:
            raise ProviderUnavailable(
                f"Ollama does not have the model '{self._model}'. "
                f"Run: ollama pull {self._model}",
                provider=self.name,
                model=self._model,
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"Ollama returned {response.status_code}: {response.text[:300]}",
                provider=self.name,
            )

        body = response.json()
        return ProviderResponse(
            text=(body.get("message", {}).get("content") or "").strip(),
            provider=self.name,
            model=body.get("model") or self._model,
            usage=Usage(
                input_tokens=int(body.get("prompt_eval_count") or 0),
                output_tokens=int(body.get("eval_count") or 0),
            ),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def health(self) -> ProviderHealth:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
            response.raise_for_status()
            names = {
                m.get("name", "") for m in (response.json().get("models") or [])
            }
            # Ollama reports "llama3.2:latest"; users configure "llama3.2".
            has_model = any(
                name == self._model or name.split(":")[0] == self._model.split(":")[0]
                for name in names
            )
            return ProviderHealth(
                name=self.name,
                configured=True,  # local provider needs no credentials
                reachable=has_model,
                model=self._model,
                detail=(
                    ""
                    if has_model
                    else f"Ollama is up but '{self._model}' is not pulled. "
                    f"Run: ollama pull {self._model}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return ProviderHealth(
                name=self.name,
                configured=True,
                reachable=False,
                model=self._model,
                detail=f"Cannot reach Ollama at {self._base_url}: {str(exc)[:160]}",
            )


def ollama_provider() -> OllamaProvider:
    return OllamaProvider()
