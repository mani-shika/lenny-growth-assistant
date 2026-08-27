"""Provider abstraction, the model toggle, and the fallback chain.

No network. Each provider is driven through a stubbed transport, which is what
lets us assert the failure behaviour that matters operationally: a down Ollama,
a missing key, a rate limit, and an empty completion must each be distinguished
and handled, not collapsed into "something went wrong".
"""

from __future__ import annotations

import httpx
import pytest

from app.agent import registry
from app.agent.providers.ollama_provider import OllamaProvider
from app.agent.providers.openai_compat import OpenAICompatProvider
from app.agent.types import ChatMessage, ProviderResponse, Role, Usage
from app.core.config import Settings
from app.core.errors import (
    AllProvidersFailed,
    ProviderNotConfigured,
    ProviderTimeout,
    ProviderUnavailable,
)

MESSAGES = [ChatMessage(Role.USER, "hello")]

# Captured before any monkeypatching: the fixture below replaces
# httpx.AsyncClient, so constructing one through the patched name would recurse.
_RealAsyncClient = httpx.AsyncClient


def _client(handler):  # noqa: ANN001, ANN202
    return _RealAsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def patch_httpx(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Swap httpx.AsyncClient for one backed by a mock transport."""

    def apply(handler) -> None:  # noqa: ANN001
        def factory(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return _client(handler)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return apply


# --------------------------------------------------------------------------
# Configuration: the model toggle
# --------------------------------------------------------------------------


def test_active_provider_is_always_first_in_the_chain() -> None:
    settings = Settings(llm_provider="groq", llm_fallback_chain="ollama,groq")
    assert settings.fallback_chain[0] == "groq"


def test_chain_deduplicates_and_drops_unknown_providers() -> None:
    settings = Settings(
        llm_provider="ollama", llm_fallback_chain="ollama,groq,ollama,nonsense"
    )
    assert settings.fallback_chain == ["ollama", "groq"]


def test_ollama_needs_no_credentials_but_cloud_providers_do() -> None:
    settings = Settings(llm_provider="ollama", groq_api_key="", anthropic_api_key="k")
    assert settings.is_configured("ollama") is True
    assert settings.is_configured("groq") is False
    assert settings.is_configured("anthropic") is True


def test_switching_provider_needs_no_code_change() -> None:
    """The toggle requirement, stated as a test."""
    for name in ("ollama", "groq", "openai", "anthropic"):
        provider = registry.get_provider(name)
        assert provider.name == name
        assert provider.model


def test_unknown_provider_is_rejected_clearly() -> None:
    with pytest.raises(ProviderNotConfigured):
        registry.get_provider("gpt5-turbo-ultra")


# --------------------------------------------------------------------------
# Ollama - the local provider
# --------------------------------------------------------------------------


async def test_ollama_parses_a_completion_and_its_token_counts(patch_httpx) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={
                "model": "llama3.2",
                "message": {"role": "assistant", "content": "  An answer.  "},
                "prompt_eval_count": 120,
                "eval_count": 30,
            },
        )

    patch_httpx(handler)
    response = await OllamaProvider().complete(MESSAGES)

    assert response.text == "An answer."
    assert response.provider == "ollama"
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 30
    assert response.latency_ms >= 0


async def test_missing_ollama_model_names_the_pull_command(patch_httpx) -> None:  # noqa: ANN001
    """The single most common setup failure deserves an actionable message."""
    patch_httpx(lambda _r: httpx.Response(404, json={"error": "model not found"}))

    with pytest.raises(ProviderUnavailable) as excinfo:
        await OllamaProvider(model="llama3.2").complete(MESSAGES)

    assert "ollama pull llama3.2" in str(excinfo.value)


async def test_unreachable_ollama_is_reported_as_unavailable(patch_httpx) -> None:  # noqa: ANN001
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    patch_httpx(handler)
    with pytest.raises(ProviderUnavailable) as excinfo:
        await OllamaProvider().complete(MESSAGES)

    assert "ollama serve" in str(excinfo.value).lower()


async def test_ollama_timeout_is_distinguished_from_being_down(patch_httpx) -> None:  # noqa: ANN001
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    patch_httpx(handler)
    with pytest.raises(ProviderTimeout):
        await OllamaProvider().complete(MESSAGES)


async def test_ollama_health_detects_a_model_that_is_not_pulled(patch_httpx) -> None:  # noqa: ANN001
    patch_httpx(
        lambda _r: httpx.Response(200, json={"models": [{"name": "mistral:latest"}]})
    )
    health = await OllamaProvider(model="llama3.2").health()

    assert health.configured is True
    assert health.reachable is False
    assert "ollama pull llama3.2" in health.detail


async def test_ollama_health_matches_the_latest_tag(patch_httpx) -> None:  # noqa: ANN001
    """Ollama reports "llama3.2:latest"; operators configure "llama3.2"."""
    patch_httpx(
        lambda _r: httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}]})
    )
    assert (await OllamaProvider(model="llama3.2").health()).reachable is True


async def test_health_never_raises_even_when_everything_is_down(patch_httpx) -> None:  # noqa: ANN001
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    patch_httpx(handler)
    health = await OllamaProvider().health()
    assert health.reachable is False


# --------------------------------------------------------------------------
# OpenAI-compatible providers (Groq, OpenAI)
# --------------------------------------------------------------------------


def _openai_provider() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="groq", base_url="https://api.test/v1", api_key="k", model="m"
    )


async def test_openai_compatible_completion(patch_httpx) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "Hi there."}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    patch_httpx(handler)
    response = await _openai_provider().complete(MESSAGES)

    assert response.text == "Hi there."
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 4
    assert response.usage.to_dict()["total_tokens"] == 14


async def test_missing_key_fails_before_any_network_call() -> None:
    provider = OpenAICompatProvider(
        name="groq", base_url="https://api.test/v1", api_key="", model="m"
    )
    with pytest.raises(ProviderNotConfigured):
        await provider.complete(MESSAGES)


async def test_rate_limit_is_retryable_so_the_chain_advances(patch_httpx) -> None:  # noqa: ANN001
    patch_httpx(lambda _r: httpx.Response(429, json={"error": "slow down"}))
    with pytest.raises(ProviderUnavailable):
        await _openai_provider().complete(MESSAGES)


async def test_rejected_key_is_reported_as_configuration_not_an_outage(patch_httpx) -> None:  # noqa: ANN001
    patch_httpx(lambda _r: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ProviderNotConfigured):
        await _openai_provider().complete(MESSAGES)


# --------------------------------------------------------------------------
# The fallback chain
# --------------------------------------------------------------------------


class _StubProvider:
    """A provider that behaves however the test needs it to."""

    def __init__(self, name: str, *, outcome: str) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.outcome = outcome
        self.calls = 0

    async def complete(self, _messages, **_kwargs):  # noqa: ANN001, ANN202
        self.calls += 1
        if self.outcome == "down":
            raise ProviderUnavailable(f"{self.name} is down", provider=self.name)
        if self.outcome == "timeout":
            raise ProviderTimeout(f"{self.name} timed out", provider=self.name)
        if self.outcome == "empty":
            return ProviderResponse(text="   ", provider=self.name, model=self.model)
        if self.outcome == "boom":
            raise RuntimeError("a genuine bug, not an outage")
        return ProviderResponse(
            text=f"answer from {self.name}",
            provider=self.name,
            model=self.model,
            usage=Usage(1, 2),
        )


def _install(monkeypatch: pytest.MonkeyPatch, providers: dict[str, _StubProvider]) -> None:
    monkeypatch.setattr(registry, "get_provider", lambda name=None: providers[name])
    monkeypatch.setattr(registry, "active_chain", lambda: list(providers))


async def test_first_healthy_provider_wins_and_others_are_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = {
        "ollama": _StubProvider("ollama", outcome="ok"),
        "groq": _StubProvider("groq", outcome="ok"),
    }
    _install(monkeypatch, providers)

    response, attempts = await registry.generate(MESSAGES)

    assert response.provider == "ollama"
    assert providers["groq"].calls == 0
    assert len(attempts) == 1


@pytest.mark.parametrize("failure", ["down", "timeout", "empty"])
async def test_chain_advances_past_each_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    providers = {
        "ollama": _StubProvider("ollama", outcome=failure),
        "groq": _StubProvider("groq", outcome="ok"),
    }
    _install(monkeypatch, providers)

    response, attempts = await registry.generate(MESSAGES)

    assert response.provider == "groq"
    assert len(attempts) == 2
    assert attempts[0]["outcome"] in {"failed", "empty"}
    assert attempts[1]["outcome"] == "ok"


async def test_attempt_trail_records_why_each_provider_was_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Why did this answer come from Groq?" must be answerable after the fact."""
    providers = {
        "ollama": _StubProvider("ollama", outcome="down"),
        "groq": _StubProvider("groq", outcome="ok"),
    }
    _install(monkeypatch, providers)

    _, attempts = await registry.generate(MESSAGES)

    assert attempts[0]["provider"] == "ollama"
    assert attempts[0]["code"] == "provider_unavailable"
    assert "is down" in attempts[0]["error"]


async def test_all_providers_failing_raises_with_the_full_trail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = {
        "ollama": _StubProvider("ollama", outcome="down"),
        "groq": _StubProvider("groq", outcome="timeout"),
    }
    _install(monkeypatch, providers)

    with pytest.raises(AllProvidersFailed) as excinfo:
        await registry.generate(MESSAGES)

    assert len(excinfo.value.details["attempts"]) == 2


async def test_a_genuine_bug_is_not_swallowed_by_the_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback covers outages, not defects.

    Silently retrying a TypeError on the next provider would turn a bug into a
    latency problem and hide it from whoever has to fix it.
    """
    providers = {
        "ollama": _StubProvider("ollama", outcome="boom"),
        "groq": _StubProvider("groq", outcome="ok"),
    }
    _install(monkeypatch, providers)

    with pytest.raises(RuntimeError):
        await registry.generate(MESSAGES)
    assert providers["groq"].calls == 0


async def test_provider_override_bypasses_the_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = {
        "ollama": _StubProvider("ollama", outcome="ok"),
        "groq": _StubProvider("groq", outcome="ok"),
    }
    _install(monkeypatch, providers)

    response, _ = await registry.generate(MESSAGES, provider_override="groq")

    assert response.provider == "groq"
    assert providers["ollama"].calls == 0
