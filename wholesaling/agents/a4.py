"""A4 Reply & Buy Box (REQUIREMENTS.md AGT-A4, INV-13..15, INV-22, XC-07; build step BO-07).

Three model tasks on one ``BaseAgent``:

- ``classify``        latest inbound message -> one of the eight INV-13 classes
- ``extract_buy_box`` latest inbound message -> zero or more structured buy boxes, with the
                      fields the investor did *not* state tracked as ``unknowns`` and any
                      change against the current version recorded as ``corrections``
- ``draft_reply``     thread + classification + buy box -> a reply that asks only for the
                      missing fields that matter and flags price/contract/legal content

Deterministic work happens around the model, not in it:

- quoted copies of earlier emails are stripped before classification (``strip_quoted_text``,
  Outlook and Gmail reply markers);
- automatic replies are detected from headers / subject / body (``detect_auto_reply``) and
  forced to ``ambiguous`` with ``is_auto_reply=True`` (INV-22 out-of-office case);
- unknown tracking and correction history are computed by comparing the extraction against the
  current buy box, so the record is right even when the model forgets to list them;
- the extraction schema has no defaults and every criterion is nullable with ``null`` as the
  *first* option, so an unscripted mock (and a model that follows the schema) invents nothing.

Routing: ``classify`` runs on Sonnet via a per-agent override (``MODEL_OVERRIDES``), as section
5 lists A4 as a Sonnet agent; extraction stays on the Haiku default (docs/agents/llm.md).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.base import BaseAgent
from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import Overrides
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.prompts import a4 as prompts
from wholesaling.db.models.enums import AgentId, MessageClassification
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria

# Section 5 lists A4 as a Sonnet agent; routing is by task kind (classify -> Haiku), so the
# classify step is moved to Sonnet with the router's per-agent override mechanism. Extraction
# stays on Haiku. ``build_runner(settings, overrides=MODEL_OVERRIDES)`` applies it.
MODEL_OVERRIDES: Overrides = {AgentId.A4.value: {TaskKind.CLASSIFY.value: "claude-sonnet-5-5"}}

# The buy-box fields A4 tracks as "unknown" until the investor states them (INV-14). These are
# the ones that matter for sending matching deals; everything else is optional colour.
TRACKED_BUY_BOX_FIELDS: tuple[str, ...] = (
    "geography",
    "asset_types",
    "price_max",
    "condition",
    "financing",
    "closing_timeline",
)

SUBSTANTIVE_CLASSES: frozenset[MessageClassification] = frozenset(
    {
        MessageClassification.INTERESTED,
        MessageClassification.BUY_BOX_SUPPLIED,
        MessageClassification.QUESTION,
        MessageClassification.NOT_NOW,
        MessageClassification.NOT_INTERESTED,
        MessageClassification.OPT_OUT,
        MessageClassification.AMBIGUOUS,
    }
)
"""Every class except ``unrelated``; auto-replies are excluded separately (``is_auto_reply``)."""

REPLY_WARRANTED_CLASSES: frozenset[MessageClassification] = frozenset(
    {
        MessageClassification.INTERESTED,
        MessageClassification.BUY_BOX_SUPPLIED,
        MessageClassification.QUESTION,
        MessageClassification.NOT_NOW,
        MessageClassification.AMBIGUOUS,
    }
)

BUY_BOX_CLASSES: frozenset[MessageClassification] = frozenset(
    {MessageClassification.INTERESTED, MessageClassification.BUY_BOX_SUPPLIED}
)

# --------------------------------------------------------------------------------------------
# Deterministic pre-processing
# --------------------------------------------------------------------------------------------

_ORIGINAL_MESSAGE_RE = re.compile(r"^\s*-{2,}\s*(Original|Forwarded) Message\s*-{2,}\s*$", re.I)
_OUTLOOK_SEPARATOR_RE = re.compile(r"^\s*_{5,}\s*$")
_FROM_RE = re.compile(r"^\s*(\*\*)?From:(\*\*)?\s+\S", re.I)
_SENT_RE = re.compile(r"^\s*(\*\*)?(Sent|Date):(\*\*)?\s+\S", re.I)
_GMAIL_ON_RE = re.compile(r"^\s*On\s.+", re.I)
_WROTE_RE = re.compile(r"wrote:\s*$", re.I)
_QUOTE_LINE_RE = re.compile(r"^\s*>")
_HEADER_LOOKAHEAD = 4
_WROTE_LOOKAHEAD = 3


@dataclass(frozen=True, slots=True)
class QuoteStripResult:
    text: str
    stripped: bool
    marker: str | None = None


def _quote_start(lines: Sequence[str]) -> tuple[int, str] | None:
    """Index of the first line that begins quoted history, and which marker matched."""
    for i, line in enumerate(lines):
        if _ORIGINAL_MESSAGE_RE.match(line):
            return i, "original_message"
        if _QUOTE_LINE_RE.match(line):
            return i, "angle_quote"
        if _OUTLOOK_SEPARATOR_RE.match(line):
            window = lines[i + 1 : i + 1 + _HEADER_LOOKAHEAD]
            if any(_FROM_RE.match(w) for w in window):
                return i, "outlook_separator"
        if _FROM_RE.match(line):
            window = lines[i + 1 : i + 1 + _HEADER_LOOKAHEAD]
            if any(_SENT_RE.match(w) for w in window):
                return i, "outlook_header"
        if _GMAIL_ON_RE.match(line):
            window = lines[i : i + _WROTE_LOOKAHEAD]
            if any(_WROTE_RE.search(w) for w in window):
                return i, "gmail_wrote"
    return None


def strip_quoted_text(body: str) -> QuoteStripResult:
    """Remove quoted earlier emails from a reply body.

    Cuts at the first Outlook (``-----Original Message-----``, the ``____`` separator, a
    ``From:/Sent:/To:/Subject:`` header block) or Gmail (``On <date> <name> wrote:``, ``>``
    quoted lines) marker. If stripping would leave nothing (the reply *is* the marker), the
    original text is returned untouched so no content is lost. Signatures are left alone.
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    found = _quote_start(lines)
    if found is None:
        return QuoteStripResult(text=normalized.strip(), stripped=False)
    index, marker = found
    head = "\n".join(lines[:index]).strip()
    if not head:
        return QuoteStripResult(text=normalized.strip(), stripped=False, marker=marker)
    return QuoteStripResult(text=head, stripped=True, marker=marker)


_AUTO_SUBJECT_RE = re.compile(
    r"^\s*(re:\s*)?(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|autoreply|"
    r"auto response|away from (the )?office)",
    re.I,
)
_AUTO_BODY_RE = re.compile(
    r"\b(out of (the )?office|automatic reply|auto-?reply|currently (away|out of the office|"
    r"on leave|travel+ing)|limited access to (my )?e-?mail|will (respond|reply) (when|upon|on) "
    r"(my|I) return|on (vacation|holiday|leave) until)\b",
    re.I,
)


def detect_auto_reply(
    *, subject: str | None, body: str, headers: Mapping[str, str] | None = None
) -> bool:
    """Header, subject or body evidence that this is an automatic (out-of-office) reply."""
    lowered = {k.lower(): v.lower() for k, v in (headers or {}).items()}
    auto_submitted = lowered.get("auto-submitted", "")
    if auto_submitted and auto_submitted != "no":
        return True
    if "x-auto-response-suppress" in lowered or "x-autoreply" in lowered:
        return True
    if subject and _AUTO_SUBJECT_RE.match(subject):
        return True
    return bool(_AUTO_BODY_RE.search(body))


# --------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThreadMessage:
    """One message of the thread as the agent sees it (a projection of ``messages`` rows)."""

    direction: str  # "in" | "out"
    body: str
    from_address: str | None = None
    subject: str | None = None
    at: datetime | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    is_latest_inbound: bool = False


def latest_inbound(thread: Sequence[ThreadMessage]) -> ThreadMessage:
    flagged = [m for m in thread if m.is_latest_inbound]
    if flagged:
        return flagged[-1]
    inbound = [m for m in thread if m.direction == "in"]
    if not inbound:
        msg = "thread has no inbound message to classify"
        raise ValueError(msg)
    return inbound[-1]


def render_thread(thread: Sequence[ThreadMessage], *, exclude_latest: bool = True) -> str:
    """Thread history as plain text for the prompt, oldest first, latest inbound excluded."""
    latest = latest_inbound(thread) if exclude_latest else None
    parts: list[str] = []
    for i, m in enumerate(thread, start=1):
        if latest is not None and m is latest:
            continue
        who = "US" if m.direction == "out" else "INVESTOR"
        when = m.at.strftime("%Y-%m-%d %H:%M") if m.at else "unknown time"
        sender = f" <{m.from_address}>" if m.from_address else ""
        body = strip_quoted_text(m.body).text if m.direction == "in" else m.body.strip()
        parts.append(f"[{i}] {who}{sender} at {when}:\n{body}")
    return "\n\n".join(parts) if parts else "(no earlier messages)"


# --------------------------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class A4Classification:
    classification: MessageClassification
    confidence: float
    rationale: str
    is_auto_reply: bool
    quoted_text_stripped: bool
    reconnect_date: date | None = None
    run_id: uuid.UUID | None = None  # None when no model call was made (auto-reply short-cut)

    @property
    def is_substantive(self) -> bool:
        """Every class except ``unrelated``; auto-replies are never substantive (INV-22)."""
        return not self.is_auto_reply and self.classification in SUBSTANTIVE_CLASSES

    @property
    def warrants_reply(self) -> bool:
        return not self.is_auto_reply and self.classification in REPLY_WARRANTED_CLASSES

    @property
    def may_contain_buy_box(self) -> bool:
        return not self.is_auto_reply and self.classification in BUY_BOX_CLASSES

    def to_json(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "is_auto_reply": self.is_auto_reply,
            "quoted_text_stripped": self.quoted_text_stripped,
            "reconnect_date": self.reconnect_date.isoformat() if self.reconnect_date else None,
            "run_id": str(self.run_id) if self.run_id else None,
        }


@dataclass(frozen=True, slots=True)
class Correction:
    field: str
    old: Any
    new: Any

    def to_json(self) -> dict[str, Any]:
        return {"field": self.field, "old": _jsonable(self.old), "new": _jsonable(self.new)}


@dataclass(frozen=True, slots=True)
class ExtractedBuyBox:
    buy_box_key: str
    criteria: BuyBoxCriteria
    unknowns: list[str]
    corrections: list[Correction]
    raw_text: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "buy_box_key": self.buy_box_key,
            "criteria": {k: _jsonable(v) for k, v in asdict(self.criteria).items()},
            "unknowns": list(self.unknowns),
            "corrections": [c.to_json() for c in self.corrections],
            "raw_text": self.raw_text,
        }


@dataclass(frozen=True, slots=True)
class BuyBoxExtraction:
    buy_boxes: list[ExtractedBuyBox]
    unknowns: list[str]
    corrections: list[Correction]
    run_id: uuid.UUID

    @property
    def found_any(self) -> bool:
        return bool(self.buy_boxes)

    def to_json(self) -> dict[str, Any]:
        return {
            "buy_boxes": [b.to_json() for b in self.buy_boxes],
            "unknowns": list(self.unknowns),
            "corrections": [c.to_json() for c in self.corrections],
            "run_id": str(self.run_id),
        }


@dataclass(frozen=True, slots=True)
class ReplyDraft:
    subject: str
    body: str
    asks_for: list[str]
    contains_sensitive: bool
    run_id: uuid.UUID

    def to_json(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "asks_for": list(self.asks_for),
            "contains_sensitive": self.contains_sensitive,
            "run_id": str(self.run_id),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        # ``Money`` columns come back as ``250000.00``; keep one canonical spelling.
        return format(value.normalize(), "f")
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


# --------------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------------


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """Nullable with ``null`` first: the mock's placeholder picks the first option, so an
    unscripted run yields ``null`` rather than an invented value."""
    return {"anyOf": [{"type": "null"}, schema]}


CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": list(prompts.CLASSES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "is_auto_reply": {"type": "boolean"},
        "reconnect_date": _nullable({"type": "string", "description": "ISO 8601 date"}),
    },
    "required": ["classification", "confidence", "rationale", "is_auto_reply", "reconnect_date"],
    "additionalProperties": False,
}

_STRING_LIST = {"type": "array", "items": {"type": "string"}}
_CORRECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "old": _nullable({"type": "string"}),
        "new": _nullable({"type": "string"}),
    },
    "required": ["field", "old", "new"],
    "additionalProperties": False,
}
BUY_BOX_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "buy_box_key": {"type": "string"},
        "geography": _nullable(
            {
                "type": "object",
                "properties": {
                    "markets": _nullable(_STRING_LIST),
                    "cities": _nullable(_STRING_LIST),
                    "zips": _nullable(_STRING_LIST),
                    "neighborhoods": _nullable(_STRING_LIST),
                },
                "required": ["markets", "cities", "zips", "neighborhoods"],
                "additionalProperties": False,
            }
        ),
        "asset_types": _nullable(_STRING_LIST),
        "price_min": _nullable({"type": "number"}),
        "price_max": _nullable({"type": "number"}),
        "condition": _nullable({"type": "string"}),
        "strategy": _nullable({"type": "string"}),
        "financing": _nullable({"type": "string"}),
        "closing_timeline": _nullable({"type": "string"}),
        "other_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                "required": ["name", "value"],
                "additionalProperties": False,
            },
        },
        "corrections": {"type": "array", "items": _CORRECTION_SCHEMA},
        "raw_text": _nullable({"type": "string"}),
    },
    "required": [
        "buy_box_key",
        "geography",
        "asset_types",
        "price_min",
        "price_max",
        "condition",
        "strategy",
        "financing",
        "closing_timeline",
        "other_criteria",
        "corrections",
        "raw_text",
    ],
    "additionalProperties": False,
}
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "buy_boxes": {"type": "array", "items": BUY_BOX_ITEM_SCHEMA},
        "rationale": {"type": "string"},
    },
    "required": ["buy_boxes", "rationale"],
    "additionalProperties": False,
}

DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "asks_for": _STRING_LIST,
        "contains_sensitive": {"type": "boolean"},
    },
    "required": ["subject", "body", "asks_for", "contains_sensitive"],
    "additionalProperties": False,
}

# Price / contract / legal content a reply must not engage with at L1 (POL-03c, POL-09).
SENSITIVE_CONTENT_RE = re.compile(
    r"(\$\s?\d|\b\d+\s?k\b|\b(price|pricing|offer(s|ed|ing)?|contract(s|ed)?|earnest|escrow|"
    r"attorney|lawyer|legal|lawsuit|liabilit(y|ies)|assignment fee|deposit|closing costs?|"
    r"commission|guarantee(d|s)?|negotiat(e|ion|ing)|terms)\b)",
    re.I,
)


def contains_sensitive_content(text: str) -> bool:
    return bool(SENSITIVE_CONTENT_RE.search(text))


# --------------------------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------------------------


class A4ReplyAgent(BaseAgent):
    agent_id = AgentId.A4
    PROMPT_VERSION = prompts.PROMPT_VERSION
    SYSTEM_PROMPT = prompts.SYSTEM_PROMPT
    DEFAULT_MAX_TOKENS = 1500

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

    # -- prompt builders (public so tests can assert on their content) -----------------------

    def classify_prompt(self, thread: Sequence[ThreadMessage]) -> tuple[str, QuoteStripResult]:
        latest = latest_inbound(thread)
        stripped = strip_quoted_text(latest.body)
        content = (
            f"{prompts.CLASSIFY_INSTRUCTIONS}\n\n"
            f"THREAD HISTORY (oldest first):\n{render_thread(thread)}\n\n"
            f"LATEST INBOUND MESSAGE from {latest.from_address or 'unknown'}"
            f" (subject: {latest.subject or '(none)'}):\n{stripped.text}"
        )
        return content, stripped

    def extract_prompt(
        self,
        thread: Sequence[ThreadMessage],
        current: Mapping[str, BuyBoxCriteria] | None,
    ) -> str:
        latest = latest_inbound(thread)
        stripped = strip_quoted_text(latest.body)
        current_text = (
            "\n".join(
                f"- {key}: {self.as_json(_criteria_json(c))}" for key, c in (current or {}).items()
            )
            or "(none on record)"
        )
        return (
            f"{prompts.EXTRACT_INSTRUCTIONS}\n\n"
            f"CURRENT BUY BOX(ES) ON RECORD:\n{current_text}\n\n"
            f"THREAD HISTORY (oldest first):\n{render_thread(thread)}\n\n"
            f"LATEST INBOUND MESSAGE:\n{stripped.text}"
        )

    def draft_prompt(
        self,
        thread: Sequence[ThreadMessage],
        classification: A4Classification,
        buy_box: BuyBoxCriteria | None,
        missing_fields: Sequence[str],
        *,
        facts: Mapping[str, Any] | None = None,
    ) -> str:
        latest = latest_inbound(thread)
        stripped = strip_quoted_text(latest.body)
        return (
            f"{prompts.DRAFT_INSTRUCTIONS}\n\n"
            f"CLASSIFICATION: {classification.classification.value}"
            f" (confidence {classification.confidence:.2f})"
            + (
                f"; reconnect date {classification.reconnect_date.isoformat()}"
                if classification.reconnect_date
                else ""
            )
            + "\n\n"
            f"FACTS WE KNOW:\n{self.as_json(dict(facts or {}))}\n\n"
            f"CURRENT BUY BOX:\n"
            f"{self.as_json(_criteria_json(buy_box)) if buy_box else '(none yet)'}\n\n"
            f"MISSING FIELDS YOU MAY ASK ABOUT: {', '.join(missing_fields) or '(none)'}\n\n"
            f"THREAD HISTORY (oldest first):\n{render_thread(thread)}\n\n"
            f"LATEST INBOUND MESSAGE from {latest.from_address or 'unknown'}"
            f" (subject: {latest.subject or '(none)'}):\n{stripped.text}"
        )

    # -- tasks -------------------------------------------------------------------------------

    async def classify(  # type: ignore[override]
        self, thread_messages: Sequence[ThreadMessage]
    ) -> A4Classification:
        """Classify the latest inbound message of ``thread_messages`` (INV-13)."""
        latest = latest_inbound(thread_messages)
        content, stripped = self.classify_prompt(thread_messages)
        if detect_auto_reply(subject=latest.subject, body=stripped.text, headers=latest.headers):
            # No model call: an automatic reply is never a human answer (INV-22).
            return A4Classification(
                classification=MessageClassification.AMBIGUOUS,
                confidence=1.0,
                rationale="automatic reply detected from headers/subject/body",
                is_auto_reply=True,
                quoted_text_stripped=stripped.stripped,
            )
        request = self.build_request(
            TaskKind.CLASSIFY, content, json_schema=CLASSIFY_SCHEMA, max_tokens=400
        )
        response, run_id = await self.run(TaskKind.CLASSIFY, request)
        parsed = response.parsed
        if parsed is None:
            msg = "A4 classify returned no structured output"
            raise StructuredOutputError(msg)
        label = str(parsed.get("classification", ""))
        try:
            classification = MessageClassification(label)
        except ValueError as exc:
            msg = f"A4 classify returned unknown class {label!r}"
            raise StructuredOutputError(msg) from exc
        is_auto = bool(parsed.get("is_auto_reply", False))
        if is_auto:
            classification = MessageClassification.AMBIGUOUS
        confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.0))))
        reconnect = (
            _parse_date(parsed.get("reconnect_date"))
            if classification is MessageClassification.NOT_NOW
            else None
        )
        return A4Classification(
            classification=classification,
            confidence=confidence,
            rationale=str(parsed.get("rationale", "")),
            is_auto_reply=is_auto,
            quoted_text_stripped=stripped.stripped,
            reconnect_date=reconnect,
            run_id=run_id,
        )

    async def extract_buy_box(
        self,
        thread_messages: Sequence[ThreadMessage],
        current_buy_box: BuyBoxCriteria | Mapping[str, BuyBoxCriteria] | None = None,
    ) -> BuyBoxExtraction:
        """Extract the buy box(es) stated in the latest inbound message (INV-14).

        ``current_buy_box`` is the current version (or a ``{key: criteria}`` mapping when the
        investor already has several). Unknowns and corrections are computed here against it.
        """
        current = _as_mapping(current_buy_box)
        content = self.extract_prompt(thread_messages, current)
        request = self.build_request(TaskKind.EXTRACT, content, json_schema=EXTRACT_SCHEMA)
        response, run_id = await self.run(TaskKind.EXTRACT, request)
        parsed = response.parsed
        if parsed is None:
            msg = "A4 extract returned no structured output"
            raise StructuredOutputError(msg)
        items = parsed.get("buy_boxes") or []
        boxes: list[ExtractedBuyBox] = []
        all_unknowns: list[str] = []
        all_corrections: list[Correction] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            box = _extracted_box(item, current)
            if box is None:
                continue
            boxes.append(box)
            for u in box.unknowns:
                if u not in all_unknowns:
                    all_unknowns.append(u)
            all_corrections.extend(box.corrections)
        return BuyBoxExtraction(
            buy_boxes=boxes, unknowns=all_unknowns, corrections=all_corrections, run_id=run_id
        )

    async def draft_reply(
        self,
        thread: Sequence[ThreadMessage],
        classification: A4Classification,
        buy_box: BuyBoxCriteria | None,
        missing_fields: Sequence[str],
        *,
        facts: Mapping[str, Any] | None = None,
        decision: str | None = None,
    ) -> ReplyDraft:
        """Draft the reply (Sonnet). ``asks_for`` is clamped to ``missing_fields`` (INV-14) and
        ``contains_sensitive`` is the model's flag OR a deterministic price/contract/legal scan."""
        latest = latest_inbound(thread)
        content = self.draft_prompt(thread, classification, buy_box, missing_fields, facts=facts)
        request = self.build_request(TaskKind.DRAFT, content, json_schema=DRAFT_SCHEMA)
        response, run_id = await self.run(TaskKind.DRAFT, request, decision=decision)
        parsed = response.parsed
        if parsed is None:
            msg = "A4 draft returned no structured output"
            raise StructuredOutputError(msg)
        body = str(parsed.get("body", "")).strip()
        subject = str(parsed.get("subject") or "").strip() or _reply_subject(latest.subject)
        if not subject.lower().startswith("re:"):
            subject = _reply_subject(subject)
        allowed = set(missing_fields)
        asks = [str(a) for a in parsed.get("asks_for") or [] if str(a) in allowed]
        sensitive = bool(parsed.get("contains_sensitive", False)) or contains_sensitive_content(
            body
        )
        return ReplyDraft(
            subject=subject,
            body=body,
            asks_for=asks,
            contains_sensitive=sensitive,
            run_id=run_id,
        )


# --------------------------------------------------------------------------------------------
# Post-processing helpers
# --------------------------------------------------------------------------------------------


def _reply_subject(subject: str | None) -> str:
    base = (subject or "").strip()
    if base.lower().startswith("re:"):
        return base
    return f"RE: {base}".strip()


def _parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_mapping(
    current: BuyBoxCriteria | Mapping[str, BuyBoxCriteria] | None,
) -> dict[str, BuyBoxCriteria]:
    if current is None:
        return {}
    if isinstance(current, BuyBoxCriteria):
        return {"default": current}
    return dict(current)


def _criteria_json(c: BuyBoxCriteria | None) -> dict[str, Any]:
    if c is None:
        return {}
    return {k: _jsonable(v) for k, v in asdict(c).items() if v not in (None, [], {})}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or None
    return None


def _geography(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("markets", "cities", "zips", "neighborhoods"):
        items = _string_list(value.get(key))
        if items:
            out[key] = items
    return out or None


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or "default"


def _extracted_box(
    item: Mapping[str, Any], current: Mapping[str, BuyBoxCriteria]
) -> ExtractedBuyBox | None:
    key = _slug(item.get("buy_box_key"))
    closing_timeline = item.get("closing_timeline")
    other = {
        str(o.get("name")): o.get("value")
        for o in item.get("other_criteria") or []
        if isinstance(o, dict) and o.get("name")
    }
    stated = BuyBoxCriteria(
        geography=_geography(item.get("geography")),
        asset_types=_string_list(item.get("asset_types")),
        price_min=_decimal(item.get("price_min")),
        price_max=_decimal(item.get("price_max")),
        condition=_opt_str(item.get("condition")),
        strategy=_opt_str(item.get("strategy")),
        financing=_opt_str(item.get("financing")),
        closing_preferences={"timeline": closing_timeline} if closing_timeline else None,
        other_criteria=other or None,
        raw_text=_opt_str(item.get("raw_text")),
    )
    if not _has_any_criterion(stated):
        return None  # the model found nothing concrete; never record an empty box

    base = current.get(key)
    if base is None and len(current) == 1 and key == "default":
        base = next(iter(current.values()))
    effective = stated.merged_over(base) if base is not None else stated
    unknowns = tracked_unknowns(effective)
    corrections = compute_corrections(base, stated) if base is not None else []
    # Keep model-reported corrections the comparison could not see (e.g. a field it changed
    # on a box we had no record of); deduped by field.
    seen = {c.field for c in corrections}
    for raw in item.get("corrections") or []:
        if isinstance(raw, dict) and raw.get("field") and str(raw["field"]) not in seen:
            corrections.append(Correction(str(raw["field"]), raw.get("old"), raw.get("new")))
            seen.add(str(raw["field"]))
    criteria = replace(stated, unknown_fields=unknowns)
    return ExtractedBuyBox(
        buy_box_key=key,
        criteria=criteria,
        unknowns=unknowns,
        corrections=corrections,
        raw_text=stated.raw_text,
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _has_any_criterion(c: BuyBoxCriteria) -> bool:
    return any(
        v not in (None, [], {})
        for k, v in asdict(c).items()
        if k not in ("raw_text", "unknown_fields")
    )


def _tracked_value(c: BuyBoxCriteria, field_name: str) -> Any:
    if field_name == "closing_timeline":
        return (c.closing_preferences or {}).get("timeline")
    return getattr(c, field_name)


def tracked_unknowns(c: BuyBoxCriteria) -> list[str]:
    """The INV-14 unknowns: tracked fields the investor has not stated (after merging)."""
    return [f for f in TRACKED_BUY_BOX_FIELDS if _tracked_value(c, f) in (None, [], {})]


def compute_corrections(base: BuyBoxCriteria, stated: BuyBoxCriteria) -> list[Correction]:
    """Fields stated now that differ from the current version: the correction history."""
    out: list[Correction] = []
    for name in (
        "geography",
        "asset_types",
        "price_min",
        "price_max",
        "condition",
        "strategy",
        "financing",
        "closing_preferences",
        "other_criteria",
    ):
        new = getattr(stated, name)
        if new in (None, [], {}):
            continue
        old = getattr(base, name)
        if old in (None, [], {}):
            continue
        if _norm(old) != _norm(new):
            out.append(Correction(name, old, new))
    return out


def _norm(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, list):
        return sorted(str(v).lower() for v in value)
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in sorted(value.items())}
    if isinstance(value, str):
        return value.strip().lower()
    return value


__all__ = [
    "BUY_BOX_CLASSES",
    "BUY_BOX_ITEM_SCHEMA",
    "CLASSIFY_SCHEMA",
    "DRAFT_SCHEMA",
    "EXTRACT_SCHEMA",
    "MODEL_OVERRIDES",
    "REPLY_WARRANTED_CLASSES",
    "SUBSTANTIVE_CLASSES",
    "TRACKED_BUY_BOX_FIELDS",
    "A4Classification",
    "A4ReplyAgent",
    "BuyBoxExtraction",
    "Correction",
    "ExtractedBuyBox",
    "QuoteStripResult",
    "ReplyDraft",
    "ThreadMessage",
    "compute_corrections",
    "contains_sensitive_content",
    "detect_auto_reply",
    "latest_inbound",
    "render_thread",
    "strip_quoted_text",
    "tracked_unknowns",
]
