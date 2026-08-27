"""Shared types for the agent layer.

These are the seam between "what the assistant wants to do" and "which model
happens to be serving it". Nothing here imports a provider SDK, so the
orchestrator, the skills and the tests can all be written once and run against
Ollama, Groq, OpenAI or the Claude Agent SDK unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str

    def to_wire(self) -> dict[str, str]:
        return {"role": self.role.value, "content": self.content}


class Skill(str, Enum):
    """The three things this assistant knows how to do.

    Kept deliberately small. Every new skill is a new failure mode in routing,
    so the bar for adding one is that it cannot be expressed as a variation of
    an existing skill's output format.
    """

    QA = "qa"
    SHIP30_ESSAY = "ship30_essay"
    ARTIFACT = "artifact"


@dataclass(slots=True)
class RouteDecision:
    skill: Skill
    confidence: float
    reason: str
    # Only meaningful for ARTIFACT: markdown | html
    artifact_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill.value,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "artifact_kind": self.artifact_kind,
        }


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
        }


@dataclass(slots=True)
class ProviderResponse:
    text: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: float = 0.0
    # Populated only by providers that run a real agent loop (Claude Agent SDK).
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ProviderHealth:
    name: str
    configured: bool
    reachable: bool
    model: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "reachable": self.reachable,
            "model": self.model,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ToolSpec:
    """A capability exposed to a model that can drive its own tool loop.

    The same specs are executed deterministically by the orchestrator for
    providers that cannot reliably drive one - see docs/architecture.md,
    "Agent routing".
    """

    name: str
    description: str
    input_schema: dict[str, Any]
