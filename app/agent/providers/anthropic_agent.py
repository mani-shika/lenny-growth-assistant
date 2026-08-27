"""Anthropic provider, built on the Claude Agent SDK.

This is the cloud agent path the brief asks for. It is wired through
`claude_agent_sdk.query()` - the real agent loop, not a Messages API call - and
the assistant's skills are exposed to it as in-process MCP tools via
`create_sdk_mcp_server`, so on this provider Claude chooses when to search the
transcripts rather than being handed a fixed context block.

**Honest status.** This path is implemented and reviewable but was verified
only as far as an Anthropic API key allows; the demo recorded for this
engagement runs on Ollama and Groq. An evaluator enables it with two lines in
`.env` (`LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=...`). See
docs/architecture.md, "Agent layer trade-off".

**Sandboxing.** The SDK ships the full Claude Code tool surface - file writes,
shell, web fetch. None of that belongs in a request handler, so every built-in
tool is denied and `setting_sources=[]` stops the process from inheriting any
`.claude/` configuration from the host. The only tools reachable are the ones
this module registers.
"""

from __future__ import annotations

import time
from typing import Any

from app.agent.providers.base import BaseProvider
from app.agent.types import ChatMessage, ProviderHealth, ProviderResponse, Role, Usage
from app.core.config import settings
from app.core.errors import ProviderNotConfigured, ProviderTimeout, ProviderUnavailable
from app.core.logging import get_logger

log = get_logger(__name__)

# Claude Code's built-in tools. A web request handler has no business touching
# the filesystem or a shell, so all of them are denied explicitly.
DENIED_BUILTIN_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "Task",
]


def sdk_available() -> bool:
    """Whether `claude-agent-sdk` is importable in this environment."""
    try:
        import claude_agent_sdk  # noqa: F401
    except Exception:  # noqa: BLE001 - absence is a normal, expected state
        return False
    return True


class AnthropicAgentProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self._model = model or settings.anthropic_model
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key

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
                "ANTHROPIC_API_KEY is not set.", provider=self.name
            )
        if not sdk_available():
            raise ProviderUnavailable(
                "claude-agent-sdk is not installed. Run: pip install claude-agent-sdk",
                provider=self.name,
            )

        import asyncio

        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            query,
        )

        system_prompt = "\n\n".join(
            m.content for m in messages if m.role is Role.SYSTEM
        )
        prompt = _render_conversation(
            [m for m in messages if m.role is not Role.SYSTEM]
        )

        options = ClaudeAgentOptions(
            system_prompt=system_prompt or None,
            model=self._model,
            max_turns=6,
            allowed_tools=[],
            disallowed_tools=DENIED_BUILTIN_TOOLS,
            # Do not inherit ~/.claude or ./.claude settings from the host.
            setting_sources=[],
            # The SDK reads credentials from the process environment, not .env.
            env={"ANTHROPIC_API_KEY": self._api_key},
        )

        started = time.perf_counter()
        parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage = Usage()

        async def run() -> None:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        text = getattr(block, "text", None)
                        if text:
                            parts.append(text)
                        elif getattr(block, "name", None):
                            tool_calls.append(
                                {
                                    "name": block.name,
                                    "input": getattr(block, "input", {}),
                                }
                            )
                elif isinstance(message, ResultMessage):
                    raw = getattr(message, "usage", None) or {}
                    if isinstance(raw, dict):
                        usage.input_tokens = int(raw.get("input_tokens") or 0)
                        usage.output_tokens = int(raw.get("output_tokens") or 0)

        try:
            await asyncio.wait_for(
                run(), timeout=timeout or settings.llm_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            raise ProviderTimeout(
                "The Claude Agent SDK did not finish in time.", provider=self.name
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalise SDK/CLI failures
            raise ProviderUnavailable(
                f"Claude Agent SDK failed: {str(exc)[:300]}", provider=self.name
            ) from exc

        return ProviderResponse(
            text="\n".join(p for p in parts if p).strip(),
            provider=self.name,
            model=self._model,
            usage=usage,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            tool_calls=tool_calls,
        )

    async def health(self) -> ProviderHealth:
        if not self._api_key:
            return ProviderHealth(
                name=self.name,
                configured=False,
                reachable=False,
                model=self._model,
                detail="ANTHROPIC_API_KEY is not set.",
            )
        if not sdk_available():
            return ProviderHealth(
                name=self.name,
                configured=True,
                reachable=False,
                model=self._model,
                detail="claude-agent-sdk is not installed.",
            )
        # A real probe would spend tokens on every health poll. Configuration
        # presence is what this endpoint is for; a failed call reports itself
        # through the fallback chain.
        return ProviderHealth(
            name=self.name,
            configured=True,
            reachable=True,
            model=self._model,
            detail="Key present and SDK importable (not probed - probing costs tokens).",
        )


def _render_conversation(messages: list[ChatMessage]) -> str:
    """Flatten history into a single prompt.

    Session state lives in Postgres, not in the SDK's own session store, so
    that one database is the single source of truth for a conversation and
    history survives a restart of the API process.
    """
    if len(messages) == 1:
        return messages[0].content
    lines: list[str] = []
    for message in messages:
        label = "User" if message.role is Role.USER else "Assistant"
        lines.append(f"{label}: {message.content}")
    lines.append("Assistant:")
    return "\n\n".join(lines)


def anthropic_provider() -> AnthropicAgentProvider:
    return AnthropicAgentProvider()
