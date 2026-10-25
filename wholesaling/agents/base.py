"""``BaseAgent``: the helper every agent (M1, A1 ... S6) builds on.

An agent subclass declares three class attributes:

    class A4ReplyAgent(BaseAgent):
        agent_id = AgentId.A4
        PROMPT_VERSION = "2026-10-25.1"
        SYSTEM_PROMPT = PromptTemplate("You classify investor replies for {company}. ...")

and then calls ``classify`` / ``extract`` / ``draft`` (or the generic ``run``). Every call goes
through ``AgentRunner`` so it is routed, priced and logged in ``agent_runs`` (XC-02).

Prompt discipline
    Prompts live as module constants and are versioned with ``PROMPT_VERSION``; the version is
    part of the prompt hash, so bumping it marks every subsequent run as a new prompt even if the
    text change is tiny. ``PromptTemplate`` refuses placeholder names that look like credentials
    and refuses to render values that look like secrets (XC-06: never embed credentials in agent
    prompts).
"""

from __future__ import annotations

import json
import re
import string
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.models import (
    Message,
    ModelRequest,
    ModelResponse,
    RequestMetadata,
    TaskKind,
    ToolSpec,
)
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.db.models.enums import AgentId


class CredentialInPromptError(ValueError):
    """XC-06: a prompt template or a rendered value looks like it carries a credential."""


# Placeholder names that must never appear in a prompt template.
_CREDENTIAL_NAME_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "private_key",
    "credential",
    "bearer",
    "client_secret",
)
# Matched as whole words so "author" / "authority" pass but "auth", "auth_header" do not.
_CREDENTIAL_NAME_WORDS = frozenset({"auth", "authn", "authz", "pwd", "jwt", "sid"})

# Values that look like live secrets regardless of what the placeholder is called.
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # Anthropic / OpenAI style keys
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),  # JWT
)


def looks_like_credential_name(name: str) -> bool:
    lowered = name.lower()
    if any(marker in lowered for marker in _CREDENTIAL_NAME_MARKERS):
        return True
    words = {w for w in re.split(r"[^a-z0-9]+", lowered) if w}
    return bool(words & _CREDENTIAL_NAME_WORDS)


def looks_like_credential_value(value: str) -> bool:
    return any(p.search(value) for p in _CREDENTIAL_VALUE_PATTERNS)


class PromptTemplate:
    """A ``str.format``-style template whose placeholders are validated at construction."""

    def __init__(self, template: str) -> None:
        self.template = template
        self.placeholders: tuple[str, ...] = tuple(_placeholders(template))
        bad = [p for p in self.placeholders if looks_like_credential_name(p)]
        if bad:
            msg = f"prompt template has credential-looking placeholder(s) {bad} (XC-06)"
            raise CredentialInPromptError(msg)
        if looks_like_credential_value(template):
            msg = "prompt template text contains something that looks like a credential (XC-06)"
            raise CredentialInPromptError(msg)

    def render(self, **values: Any) -> str:
        missing = [p for p in self.placeholders if p not in values]
        if missing:
            msg = f"prompt template missing values for {missing}"
            raise KeyError(msg)
        for key, value in values.items():
            if looks_like_credential_name(key):
                msg = f"refusing to render credential-looking argument {key!r} (XC-06)"
                raise CredentialInPromptError(msg)
            if isinstance(value, str) and looks_like_credential_value(value):
                msg = f"refusing to render {key!r}: value looks like a credential (XC-06)"
                raise CredentialInPromptError(msg)
        return self.template.format(**values)

    def __str__(self) -> str:
        return self.template


def _placeholders(template: str) -> list[str]:
    names: list[str] = []
    for _literal, field_name, _spec, _conv in string.Formatter().parse(template):
        if field_name is None or field_name == "":
            continue
        root = field_name.split(".")[0].split("[")[0]
        if root not in names:
            names.append(root)
    return names


@dataclass(frozen=True, slots=True)
class Classification:
    label: str
    confidence: float
    rationale: str
    run_id: uuid.UUID
    response: ModelResponse


def classification_schema(labels: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": list(labels)},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
        },
        "required": ["label", "confidence", "rationale"],
        "additionalProperties": False,
    }


class BaseAgent:
    """Subclass per agent. ``agent_id`` and ``SYSTEM_PROMPT`` are required."""

    agent_id: ClassVar[AgentId]
    PROMPT_VERSION: ClassVar[str] = "0"
    SYSTEM_PROMPT: ClassVar[PromptTemplate | str] = ""
    DEFAULT_MAX_TOKENS: ClassVar[int] = 1024

    def __init__(
        self,
        runner: AgentRunner,
        session: AsyncSession,
        *,
        correlation_id: str | None = None,
        prompt_values: Mapping[str, Any] | None = None,
    ) -> None:
        if not hasattr(type(self), "agent_id"):
            msg = f"{type(self).__name__} must set agent_id"
            raise TypeError(msg)
        self.runner = runner
        self.session = session
        self.correlation_id = correlation_id
        self._prompt_values = dict(prompt_values or {})

    # -- prompt --------------------------------------------------------------------------------

    def system_prompt(self, **extra: Any) -> str:
        template = type(self).SYSTEM_PROMPT
        if isinstance(template, str):
            template = PromptTemplate(template)
        values = {**self._prompt_values, **extra}
        return template.render(**values)

    def build_request(
        self,
        task_kind: TaskKind,
        user_content: str,
        *,
        json_schema: dict[str, Any] | None = None,
        tools: Sequence[ToolSpec] = (),
        history: Sequence[Message] = (),
        max_tokens: int | None = None,
        temperature: float | None = None,
        system_values: Mapping[str, Any] | None = None,
        extra_metadata: Mapping[str, Any] | None = None,
    ) -> ModelRequest:
        messages = (*history, Message(role="user", content=user_content))
        return ModelRequest(
            system=self.system_prompt(**(system_values or {})),
            messages=messages,
            metadata=RequestMetadata(
                agent=self.agent_id,
                task_kind=task_kind,
                prompt_version=type(self).PROMPT_VERSION,
                extra=dict(extra_metadata or {}),
            ),
            tools=tuple(tools),
            json_schema=json_schema,
            max_tokens=max_tokens or self.DEFAULT_MAX_TOKENS,
            temperature=temperature,
        )

    # -- calls -----------------------------------------------------------------------------------

    async def run(
        self,
        task_kind: TaskKind,
        request: ModelRequest,
        *,
        decision: str | None = None,
    ) -> tuple[ModelResponse, uuid.UUID]:
        return await self.runner.run(
            self.agent_id,
            task_kind,
            request,
            self.session,
            correlation_id=self.correlation_id,
            decision=decision,
        )

    async def classify(
        self,
        content: str,
        labels: Sequence[str],
        *,
        instructions: str | None = None,
        max_tokens: int = 256,
    ) -> Classification:
        """Pick one of ``labels`` with a confidence and a one-line rationale (Haiku by default)."""
        if not labels:
            msg = "classify needs at least one label"
            raise ValueError(msg)
        prompt = content if instructions is None else f"{instructions}\n\n{content}"
        request = self.build_request(
            TaskKind.CLASSIFY,
            prompt,
            json_schema=classification_schema(labels),
            max_tokens=max_tokens,
        )
        response, run_id = await self.run(TaskKind.CLASSIFY, request)
        parsed = _require_parsed(response)
        label = str(parsed.get("label", ""))
        if label not in labels:
            msg = f"classifier returned label {label!r} not in {list(labels)}"
            raise StructuredOutputError(msg)
        confidence = float(parsed.get("confidence", 0.0))
        return Classification(
            label=label,
            confidence=min(1.0, max(0.0, confidence)),
            rationale=str(parsed.get("rationale", "")),
            run_id=run_id,
            response=response,
        )

    async def extract(
        self,
        schema: dict[str, Any],
        content: str,
        *,
        instructions: str | None = None,
        max_tokens: int | None = None,
        task_kind: TaskKind = TaskKind.EXTRACT,
    ) -> tuple[dict[str, Any], uuid.UUID]:
        """Structured extraction into ``schema``. Also used for QA / comp-audit verdicts by
        passing ``task_kind=TaskKind.QA`` or ``TaskKind.COMP_AUDIT`` (routes to Sonnet)."""
        prompt = content if instructions is None else f"{instructions}\n\n{content}"
        request = self.build_request(task_kind, prompt, json_schema=schema, max_tokens=max_tokens)
        response, run_id = await self.run(task_kind, request)
        return _require_parsed(response), run_id

    async def draft(
        self,
        content: str,
        *,
        history: Sequence[Message] = (),
        max_tokens: int | None = None,
        temperature: float | None = None,
        decision: str | None = None,
    ) -> tuple[str, uuid.UUID]:
        """Free-text drafting (Sonnet by default). Returns ``(text, agent_run_id)``."""
        request = self.build_request(
            TaskKind.DRAFT,
            content,
            history=history,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        response, run_id = await self.run(TaskKind.DRAFT, request, decision=decision)
        return response.text, run_id

    @staticmethod
    def as_json(value: Any) -> str:
        """Stable JSON for embedding records in a user message."""
        return json.dumps(value, sort_keys=True, indent=2, default=str)


def _require_parsed(response: ModelResponse) -> dict[str, Any]:
    if response.parsed is None:
        msg = f"model {response.model} returned no structured output"
        raise StructuredOutputError(msg)
    return response.parsed


__all__ = [
    "BaseAgent",
    "Classification",
    "CredentialInPromptError",
    "PromptTemplate",
    "classification_schema",
    "looks_like_credential_name",
    "looks_like_credential_value",
]
