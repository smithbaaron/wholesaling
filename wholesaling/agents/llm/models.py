"""Provider-agnostic request / response shapes for the model layer (REQUIREMENTS.md STK-07).

Everything an agent sends to a model, and everything it gets back, is expressed with these types
so a provider (Anthropic today, Grok/OpenAI later) is a pure adapter. The ``prompt_hash`` here is
the value persisted on ``agent_runs.prompt_hash`` (XC-02): it covers the prompt version, system
prompt, messages, structured-output schema and tool definitions, so any change to what the model
sees changes the hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from wholesaling.db.models.enums import AgentId

Role = Literal["user", "assistant"]


class TaskKind(StrEnum):
    """What the agent is asking the model to do. Drives routing (ENG-Q8)."""

    CLASSIFY = "classify"
    EXTRACT = "extract"
    DRAFT = "draft"
    QA = "qa"
    COMP_AUDIT = "comp_audit"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model may call. ``input_schema`` is a JSON Schema object."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RequestMetadata:
    """Who is asking and why. ``prompt_version`` is part of the prompt hash."""

    agent: AgentId
    task_kind: TaskKind
    prompt_version: str = "0"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One completion request. Immutable; use ``dataclasses.replace`` to set ``model``."""

    system: str
    messages: tuple[Message, ...]
    metadata: RequestMetadata
    tools: tuple[ToolSpec, ...] = ()
    json_schema: dict[str, Any] | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    model: str | None = None  # filled in by the router via AgentRunner

    def __post_init__(self) -> None:
        if not self.messages:
            msg = "ModelRequest needs at least one message"
            raise ValueError(msg)
        if self.max_tokens <= 0:
            msg = "max_tokens must be positive"
            raise ValueError(msg)

    @property
    def wants_structured_output(self) -> bool:
        return self.json_schema is not None

    def hash_material(self) -> dict[str, Any]:
        """The exact material hashed into ``agent_runs.prompt_hash``."""
        return {
            "prompt_version": self.metadata.prompt_version,
            "system": self.system,
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "json_schema": self.json_schema,
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in self.tools
            ],
        }


def prompt_hash(request: ModelRequest) -> str:
    """sha256 hex digest over the canonical JSON of ``request.hash_material()``.

    Canonical = sorted keys, no whitespace variance, ``ensure_ascii`` so the digest is the
    same on every platform. The model id and ``max_tokens`` are deliberately *not* hashed: the
    hash identifies the prompt, the ``model`` column identifies the model.
    """
    canonical = json.dumps(
        request.hash_material(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(slots=True)
class ModelResponse:
    """What came back. ``parsed`` is the structured output when a ``json_schema`` was requested."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    parsed: dict[str, Any] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str | None = None
    latency_ms: int = 0
    raw: Any = None
    cost_usd: Decimal | None = None  # filled in by AgentRunner from pricing

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
