"""A5 QA (REQUIREMENTS.md AGT-A5, INV-17, INV-18, XC-07; build step BO-07).

Reviews one outbound draft with the thread history, the facts known (investor, current buy
box) and the marketing rules, and returns ``approve`` / ``block`` / ``rewrite``.

Deterministic pre-checks run **before** the model and short-circuit it. Each failure maps to
one flag and a ``block`` verdict with confidence 1.0; no ``agent_runs`` row is written because
no model was called:

| check                | flag                       | source                       |
|----------------------|----------------------------|------------------------------|
| prohibited claims    | ``prohibited_claim:<term>``| XC-07 denylist               |
| opt-out violation    | ``opt_out_violation``      | recipient on ``suppression`` |
| wrong recipient      | ``wrong_recipient``        | to != thread participant     |
| repeated request     | ``repeated_request``       | same asks_for as last outbound |
| cadence              | ``cadence_violation``      | outbound < 24 h on the thread |

Only a clean draft reaches the model (Sonnet, task kind ``qa``). Persisting the verdict is the
caller's job through ``QaReviewRepository.record`` (the review needs the draft's ``messages``
row id, which the activity owns).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.a4 import ThreadMessage, render_thread
from wholesaling.agents.base import BaseAgent
from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.prompts import a5 as prompts
from wholesaling.db.models.enums import AgentId, QaVerdict
from wholesaling.db.normalize import normalize_email

MIN_OUTBOUND_GAP = timedelta(hours=24)

# XC-07: assertions we never make unless the records support them. Matched as whole phrases,
# case-insensitively, against the draft body and subject.
PROHIBITED_CLAIMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "inventory",
        re.compile(r"\b(our|have|has|current|available)\s+inventory\b|\binventory\b", re.I),
    ),
    ("under_contract", re.compile(r"\bunder contract\b", re.I)),
    ("exclusive", re.compile(r"\bexclusiv(e|ely|ity)\b", re.I)),
    ("guaranteed", re.compile(r"\bguarantee(d|s)?\b", re.I)),
    (
        "properties_available",
        re.compile(r"\b(properties|deals|houses)\s+(we have|available now|in hand|ready)\b", re.I),
    ),
)

FLAG_OPT_OUT = "opt_out_violation"
FLAG_WRONG_RECIPIENT = "wrong_recipient"
FLAG_REPEATED_REQUEST = "repeated_request"
FLAG_CADENCE = "cadence_violation"
FLAG_PROHIBITED_PREFIX = "prohibited_claim:"


@dataclass(frozen=True, slots=True)
class QaDraft:
    """The draft under review."""

    to: Sequence[str]
    subject: str
    body: str
    asks_for: Sequence[str] = ()
    contains_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class QaInput:
    draft: QaDraft
    thread: Sequence[ThreadMessage]
    facts: Mapping[str, Any] = field(default_factory=dict)
    current_buy_box: Mapping[str, Any] | None = None
    marketing_rules: Sequence[str] = prompts.MARKETING_RULES
    recipient_suppressed: bool = False
    thread_participant: str | None = None  # the counterpart's address on the thread
    last_outbound_asks_for: Sequence[str] = ()
    last_outbound_at: datetime | None = None
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class QaResult:
    verdict: QaVerdict
    confidence: float
    flags: list[str]
    rationale: str
    rewrite_body: str | None = None
    run_id: uuid.UUID | None = None  # None when a pre-check decided (no model call)
    model: str | None = None

    @property
    def precheck_only(self) -> bool:
        return self.run_id is None

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "confidence": self.confidence,
            "flags": list(self.flags),
            "rationale": self.rationale,
            "rewrite_body": self.rewrite_body,
            "run_id": str(self.run_id) if self.run_id else None,
            "model": self.model,
        }


QA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": [v.value for v in QaVerdict]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "flags": {"type": "array", "items": {"type": "string"}},
        "rewrite_body": {"anyOf": [{"type": "null"}, {"type": "string"}]},
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "confidence", "flags", "rewrite_body", "rationale"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------------------------
# Pre-checks (pure functions, individually testable)
# --------------------------------------------------------------------------------------------


def prohibited_claim_flags(text: str) -> list[str]:
    return [f"{FLAG_PROHIBITED_PREFIX}{name}" for name, rx in PROHIBITED_CLAIMS if rx.search(text)]


def _same_address(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return normalize_email(a) == normalize_email(b)
    except ValueError:
        return a.strip().lower() == b.strip().lower()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def run_prechecks(inp: QaInput) -> list[str]:
    """Every failing deterministic check, as flags. Empty list means the model may review."""
    flags: list[str] = []
    for flag in prohibited_claim_flags(inp.draft.subject) + prohibited_claim_flags(inp.draft.body):
        if flag not in flags:
            flags.append(flag)
    if inp.recipient_suppressed:
        flags.append(FLAG_OPT_OUT)
    if inp.thread_participant is not None and (
        not inp.draft.to or not any(_same_address(t, inp.thread_participant) for t in inp.draft.to)
    ):
        flags.append(FLAG_WRONG_RECIPIENT)
    asks = {a.strip().lower() for a in inp.draft.asks_for if a.strip()}
    last = {a.strip().lower() for a in inp.last_outbound_asks_for if a.strip()}
    if asks and asks == last:
        flags.append(FLAG_REPEATED_REQUEST)
    last_at = _aware(inp.last_outbound_at)
    now = _aware(inp.now) or datetime.now(tz=UTC)
    if last_at is not None and now - last_at < MIN_OUTBOUND_GAP:
        flags.append(FLAG_CADENCE)
    return flags


# --------------------------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------------------------


class A5QaAgent(BaseAgent):
    agent_id = AgentId.A5
    PROMPT_VERSION = prompts.PROMPT_VERSION
    SYSTEM_PROMPT = prompts.SYSTEM_PROMPT
    DEFAULT_MAX_TOKENS = 1200

    DEFAULT_PROMPT_VALUES: Mapping[str, Any] = {"company": "our team"}

    def __init__(
        self,
        runner: AgentRunner,
        session: AsyncSession,
        *,
        correlation_id: str | None = None,
        prompt_values: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            runner,
            session,
            correlation_id=correlation_id,
            prompt_values={**self.DEFAULT_PROMPT_VALUES, **(prompt_values or {})},
        )

    def review_prompt(self, inp: QaInput) -> str:
        rules = "\n".join(f"{i}. {r}" for i, r in enumerate(inp.marketing_rules, start=1))
        return (
            f"{prompts.REVIEW_INSTRUCTIONS}\n\n"
            f"MARKETING RULES:\n{rules}\n\n"
            f"FACTS KNOWN:\n{self.as_json(dict(inp.facts))}\n\n"
            f"CURRENT BUY BOX:\n"
            f"{self.as_json(dict(inp.current_buy_box)) if inp.current_buy_box else '(none)'}\n\n"
            f"THREAD (oldest first, including the message being answered):\n"
            f"{render_thread(inp.thread, exclude_latest=False)}\n\n"
            f"DRAFT TO: {', '.join(inp.draft.to)}\n"
            f"DRAFT SUBJECT: {inp.draft.subject}\n"
            f"DRAFT ASKS FOR: {', '.join(inp.draft.asks_for) or '(nothing)'}\n"
            f"DRAFT FLAGGED SENSITIVE BY A4: {inp.draft.contains_sensitive}\n"
            f"DRAFT BODY:\n{inp.draft.body}"
        )

    async def review(self, inp: QaInput) -> QaResult:
        """Pre-checks first; any failure blocks without a model call. Otherwise ask the model."""
        flags = run_prechecks(inp)
        if flags:
            return QaResult(
                verdict=QaVerdict.BLOCK,
                confidence=1.0,
                flags=flags,
                rationale="blocked by deterministic pre-check: " + ", ".join(flags),
            )
        request = self.build_request(TaskKind.QA, self.review_prompt(inp), json_schema=QA_SCHEMA)
        response, run_id = await self.run(TaskKind.QA, request)
        parsed = response.parsed
        if parsed is None:
            msg = "A5 QA returned no structured output"
            raise StructuredOutputError(msg)
        try:
            verdict = QaVerdict(str(parsed.get("verdict", "")))
        except ValueError as exc:
            msg = f"A5 returned unknown verdict {parsed.get('verdict')!r}"
            raise StructuredOutputError(msg) from exc
        model_flags = [str(f) for f in parsed.get("flags") or []]
        rewrite = parsed.get("rewrite_body")
        if verdict is QaVerdict.REWRITE and not rewrite:
            # A rewrite verdict without a body cannot be acted on: a human must look.
            model_flags.append("rewrite_without_body")
        if inp.draft.contains_sensitive and "sensitive_content" not in model_flags:
            model_flags.append("sensitive_content")
        return QaResult(
            verdict=verdict,
            confidence=min(1.0, max(0.0, float(parsed.get("confidence", 0.0)))),
            flags=model_flags,
            rationale=str(parsed.get("rationale", "")),
            rewrite_body=str(rewrite) if rewrite else None,
            run_id=run_id,
            model=response.model,
        )


__all__ = [
    "FLAG_CADENCE",
    "FLAG_OPT_OUT",
    "FLAG_PROHIBITED_PREFIX",
    "FLAG_REPEATED_REQUEST",
    "FLAG_WRONG_RECIPIENT",
    "MIN_OUTBOUND_GAP",
    "PROHIBITED_CLAIMS",
    "QA_SCHEMA",
    "A5QaAgent",
    "QaDraft",
    "QaInput",
    "QaResult",
    "prohibited_claim_flags",
    "run_prechecks",
]
