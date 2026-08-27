"""The provider contract.

Two methods, deliberately. `complete()` turns messages into text; `health()`
answers "could I serve a request right now?" without spending a token. Anything
richer (streaming, tool loops, structured output) belongs to a specific
provider and must not leak into this interface, because every method added here
is a method that has to work on a 2 GB local model as well as on a frontier one.
"""

from __future__ import annotations

import abc

from app.agent.types import ChatMessage, ProviderHealth, ProviderResponse


class BaseProvider(abc.ABC):
    name: str = "base"

    @property
    @abc.abstractmethod
    def model(self) -> str: ...

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> ProviderResponse:
        """Generate a completion.

        Implementations must raise `ProviderUnavailable` / `ProviderTimeout`
        (from app.core.errors) on failure rather than returning empty text, so
        the fallback chain can tell "the model is down" from "the model had
        nothing to say".
        """

    @abc.abstractmethod
    async def health(self) -> ProviderHealth:
        """Cheap reachability probe. Must never raise."""
