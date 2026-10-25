"""Deterministic mock provider (REQUIREMENTS.md XC-11, RUL-02).

Three ways to control what comes back, checked in this order:

1. ``script_matching(predicate, ...)`` — the first matcher whose predicate accepts the request.
2. ``script(agent, task_kind, ...)`` — a response for that (agent, task kind) pair.
3. The default: when the request carries a ``json_schema`` the mock returns a valid object with
   every required key filled by a type-appropriate placeholder, so later agents' tests run with
   no script at all; otherwise it echoes a short acknowledgement.

Tokens are counted as ``len(text) // 4`` on both sides so cost arithmetic has something to chew
on. Every request is recorded in ``requests``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wholesaling.agents.llm.models import (
    ModelRequest,
    ModelResponse,
    TaskKind,
    ToolCall,
)
from wholesaling.db.models.enums import AgentId
from wholesaling.integrations.base import HealthStatus

MOCK_MODEL = "mock-model"


@dataclass(slots=True)
class ScriptedResponse:
    """What a script returns. ``raise_error`` wins over everything else."""

    text: str | None = None
    parsed: dict[str, Any] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    raise_error: Exception | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


Predicate = Callable[[ModelRequest], bool]
Responder = ScriptedResponse | Callable[[ModelRequest], ScriptedResponse]


def count_tokens(text: str) -> int:
    return len(text) // 4


def placeholder_for_schema(schema: Mapping[str, Any]) -> Any:
    """Build a value that satisfies a JSON-Schema fragment with the simplest placeholders.

    Rules: ``const`` and ``default`` win; ``enum`` picks the first member; objects fill their
    ``required`` keys (all ``properties`` when nothing is required); arrays are empty unless
    ``minItems`` demands members; strings are ``"mock"``; integers ``0``; numbers ``0.0``;
    booleans ``False``; ``null`` is ``None``. Nullable unions pick the first non-null type.
    """
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    for combinator in ("anyOf", "oneOf", "allOf"):
        options = schema.get(combinator)
        if options:
            return placeholder_for_schema(options[0])

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null = [t for t in schema_type if t != "null"]
        schema_type = non_null[0] if non_null else "null"
    if schema_type is None:
        schema_type = "object" if "properties" in schema else "string"

    if schema_type == "object":
        properties: Mapping[str, Any] = schema.get("properties", {})
        required: Sequence[str] = schema.get("required") or list(properties)
        return {key: placeholder_for_schema(properties.get(key, {})) for key in required}
    if schema_type == "array":
        min_items = int(schema.get("minItems", 0))
        item_schema = schema.get("items", {})
        return [placeholder_for_schema(item_schema) for _ in range(min_items)]
    if schema_type == "string":
        return "mock"
    if schema_type == "integer":
        return int(schema.get("minimum", 0))
    if schema_type == "number":
        return float(schema.get("minimum", 0.0))
    if schema_type == "boolean":
        return False
    return None


@dataclass(slots=True)
class _Matcher:
    predicate: Predicate
    responder: Responder


class MockModelProvider:
    name = "mock"

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.requests: list[ModelRequest] = []
        self._scripts: dict[tuple[str, str], Responder] = {}
        self._matchers: list[_Matcher] = []
        self._fail_with = fail_with
        self.healthy = True

    # -- scripting -----------------------------------------------------------------------------

    def script(
        self,
        agent: AgentId | str,
        task_kind: TaskKind | str,
        *,
        text: str | None = None,
        parsed: dict[str, Any] | None = None,
        tool_calls: Sequence[ToolCall] = (),
        raise_error: Exception | None = None,
        responder: Callable[[ModelRequest], ScriptedResponse] | None = None,
    ) -> None:
        """Register the response for one (agent, task kind). ``responder`` wins when given."""
        key = (AgentId(agent).value, TaskKind(task_kind).value)
        if responder is not None:
            self._scripts[key] = responder
            return
        self._scripts[key] = ScriptedResponse(
            text=text, parsed=parsed, tool_calls=tuple(tool_calls), raise_error=raise_error
        )

    def script_matching(self, predicate: Predicate, responder: Responder) -> None:
        """Register a response for any request the predicate accepts (checked first, in order)."""
        self._matchers.append(_Matcher(predicate, responder))

    def script_containing(self, needle: str, responder: Responder) -> None:
        """Convenience: match when ``needle`` appears in the system prompt or any message."""

        def _contains(request: ModelRequest) -> bool:
            if needle in request.system:
                return True
            return any(needle in m.content for m in request.messages)

        self.script_matching(_contains, responder)

    def reset(self) -> None:
        self.requests.clear()
        self._scripts.clear()
        self._matchers.clear()

    # -- ModelProvider ---------------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        scripted = self._resolve(request)
        if scripted.raise_error is not None:
            raise scripted.raise_error
        return self._build_response(request, scripted)

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(name=self.name, mode="mock", ok=self.healthy)

    # -- internals -------------------------------------------------------------------------------

    def _resolve(self, request: ModelRequest) -> ScriptedResponse:
        for matcher in self._matchers:
            if matcher.predicate(request):
                return _call(matcher.responder, request)
        key = (request.metadata.agent.value, request.metadata.task_kind.value)
        responder = self._scripts.get(key)
        if responder is not None:
            return _call(responder, request)
        return self._default_response(request)

    @staticmethod
    def _default_response(request: ModelRequest) -> ScriptedResponse:
        if request.json_schema is not None:
            parsed = placeholder_for_schema(request.json_schema)
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
            return ScriptedResponse(parsed=parsed)
        last = request.messages[-1].content
        return ScriptedResponse(text=f"[mock {request.metadata.task_kind.value}] {last[:80]}")

    @staticmethod
    def _build_response(request: ModelRequest, scripted: ScriptedResponse) -> ModelResponse:
        parsed = scripted.parsed
        text = scripted.text
        if text is None:
            text = json.dumps(parsed, sort_keys=True) if parsed is not None else ""
        if parsed is None and request.json_schema is not None and text:
            try:
                candidate = json.loads(text)
            except ValueError:
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate
        prompt_text = request.system + "".join(m.content for m in request.messages)
        return ModelResponse(
            text=text,
            model=request.model or MOCK_MODEL,
            input_tokens=(
                scripted.input_tokens
                if scripted.input_tokens is not None
                else count_tokens(prompt_text)
            ),
            output_tokens=(
                scripted.output_tokens if scripted.output_tokens is not None else count_tokens(text)
            ),
            parsed=parsed,
            tool_calls=scripted.tool_calls,
            stop_reason=scripted.stop_reason,
            latency_ms=0,
            raw={"mock": True},
        )


def _call(responder: Responder, request: ModelRequest) -> ScriptedResponse:
    if isinstance(responder, ScriptedResponse):
        return responder
    return responder(request)


__all__ = [
    "MOCK_MODEL",
    "MockModelProvider",
    "ScriptedResponse",
    "count_tokens",
    "placeholder_for_schema",
]
