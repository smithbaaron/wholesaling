"""S4 Seller Intake (REQUIREMENTS.md AGT-S4, SEL-06, SEL-07, SEL-08, POL-03f, POL-06, POL-09,
XC-07; build step BO-10).

Three model tasks on one ``BaseAgent`` plus deterministic form parsing:

- ``classify``   latest inbound seller message -> reply / not_interested / opt_out / unrelated
                 (Haiku), with ``is_auto_reply`` and ``asks_offer``
- ``extract``    latest inbound message -> ``IntakeFields`` (Haiku); every field nullable with
                 ``null`` first so an unscripted mock (and a schema-following model) invents
                 nothing (SEL-06 "unknowns flagged", never guessed)
- ``parse_form`` web-form payload -> ``IntakeFields`` without a model call (the form is already
                 structured)
- ``draft_reply`` thread + what is still missing -> a conversational reply asking for at most
                 two fields (SEL-07), flagged ``contains_sensitive`` when it would engage price /
                 contract / legal content (POL-06, POL-09)

Deterministic work around the model: quote stripping and auto-reply detection are A4's
(``strip_quoted_text`` / ``detect_auto_reply``); ``asks_for`` is clamped to the fields the
caller allowed and to two; the photo-upload link and the SEL-05 compliance block are appended by
``assemble_reply_body`` so neither depends on the model remembering them.

Which fields are required and which are "asked" lives in ``db.repositories.sellers``
(``missing_required``, ``unasked_optional``); choosing the next questions is ``choose_asks``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.a4 import (
    ThreadMessage,
    detect_auto_reply,
    latest_inbound,
    render_thread,
    strip_quoted_text,
)
from wholesaling.agents.base import BaseAgent
from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.prompts import s4 as prompts
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.agents.templates.seller_outreach import ensure_compliance_block
from wholesaling.db.models.enums import (
    AgentId,
    ConditionClass,
    IntakeTimeline,
    MessageClassification,
)
from wholesaling.db.repositories.sellers import IntakeFields

MAX_ASKS_PER_MESSAGE: Final[int] = 2  # SEL-07 "one or two per message"

# Human-readable labels for the field names (shown to the model under ASK FOR).
FIELD_QUESTIONS: Final[dict[str, str]] = {
    "address": "the full street address of the property (we could not match it to a parcel)",
    "motivation": "why they are thinking about selling",
    "condition": "the overall condition of the house and any known problems",
    "timeline": "how soon they would want to close",
    "co_owners": "whether anyone else is on the title",
    "occupancy": "whether the house is vacant, owner-occupied or rented",
    "liens": "whether there is a mortgage or other lien on it",
    "hoa": "whether there is an HOA",
}

# Seller-side sensitivity: dollar figures and contract / legal terms. Unlike A4 the bare word
# "offer" is allowed (the whole conversation is about a future offer); a *number* is not.
SENSITIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"(\$\s?\d|\b\d{2,3}\s?k\b|\b\d{3},\d{3}\b|\b(contract(s|ed)?|earnest|escrow|attorney|lawyer|"
    r"legal|lawsuit|liabilit(y|ies)|assignment fee|commission|guarantee(d|s)?|worth|valuation|"
    r"appraisal)\b)",
    re.I,
)


class SellerClassification(StrEnum):
    REPLY = "reply"
    NOT_INTERESTED = "not_interested"
    OPT_OUT = "opt_out"
    UNRELATED = "unrelated"

    @property
    def message_classification(self) -> MessageClassification:
        """How the row is stored in ``messages.classification`` (the shared INV-13 enum)."""
        return _TO_MESSAGE_CLASS[self]


_TO_MESSAGE_CLASS: Final[dict[SellerClassification, MessageClassification]] = {
    SellerClassification.REPLY: MessageClassification.INTERESTED,
    SellerClassification.NOT_INTERESTED: MessageClassification.NOT_INTERESTED,
    SellerClassification.OPT_OUT: MessageClassification.OPT_OUT,
    SellerClassification.UNRELATED: MessageClassification.UNRELATED,
}


def contains_sensitive_content(text: str) -> bool:
    return bool(SENSITIVE_RE.search(text))


# --------------------------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class S4Classification:
    classification: SellerClassification
    confidence: float
    rationale: str
    is_auto_reply: bool
    quoted_text_stripped: bool
    asks_offer: bool = False
    run_id: uuid.UUID | None = None

    @property
    def is_substantive(self) -> bool:
        return not self.is_auto_reply and self.classification is not SellerClassification.UNRELATED

    @property
    def warrants_intake(self) -> bool:
        return not self.is_auto_reply and self.classification is SellerClassification.REPLY

    def to_json(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "is_auto_reply": self.is_auto_reply,
            "quoted_text_stripped": self.quoted_text_stripped,
            "asks_offer": self.asks_offer,
            "run_id": str(self.run_id) if self.run_id else None,
        }


@dataclass(frozen=True, slots=True)
class S4Extraction:
    fields: IntakeFields
    rationale: str
    run_id: uuid.UUID | None  # None for a form (no model call)

    def to_json(self) -> dict[str, Any]:
        return {
            "fields": self.fields.to_json(),
            "rationale": self.rationale,
            "run_id": str(self.run_id) if self.run_id else None,
        }


@dataclass(frozen=True, slots=True)
class S4ReplyDraft:
    subject: str
    body: str  # model text only; ``assemble_reply_body`` adds the link and the compliance block
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


# --------------------------------------------------------------------------------------------
# Schemas (nullable-first: the mock's placeholder picks ``null``)
# --------------------------------------------------------------------------------------------


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [{"type": "null"}, schema]}


_STRING_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": list(prompts.CLASSES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "is_auto_reply": {"type": "boolean"},
        "asks_offer": {"type": "boolean"},
    },
    "required": ["classification", "confidence", "rationale", "is_auto_reply", "asks_offer"],
    "additionalProperties": False,
}

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "property_address": _nullable({"type": "string"}),
        "motivation": _nullable({"type": "string"}),
        "condition_class": _nullable({"type": "string", "enum": [c.value for c in ConditionClass]}),
        "disclosed_issues": _STRING_LIST,
        "timeline": _nullable(
            {
                "type": "string",
                "enum": [t.value for t in IntakeTimeline if t is not IntakeTimeline.UNKNOWN],
            }
        ),
        "timeline_text": _nullable({"type": "string"}),
        "asking_price": _nullable({"type": "number"}),
        "co_owners": _nullable({"type": "string"}),
        "occupancy": _nullable({"type": "string"}),
        "liens": _nullable({"type": "string"}),
        "hoa": _nullable({"type": "string"}),
        "seller_unsure": _STRING_LIST,
        "photos_mentioned": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": [
        "property_address",
        "motivation",
        "condition_class",
        "disclosed_issues",
        "timeline",
        "timeline_text",
        "asking_price",
        "co_owners",
        "occupancy",
        "liens",
        "hoa",
        "seller_unsure",
        "photos_mentioned",
        "rationale",
    ],
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


# --------------------------------------------------------------------------------------------
# Deterministic helpers
# --------------------------------------------------------------------------------------------


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lower().replace(",", "").replace("$", "")
    multiplier = Decimal(1)
    if text.endswith("k"):
        multiplier = Decimal(1000)
        text = text[:-1]
    try:
        amount = Decimal(text) * multiplier
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


def _condition(value: Any) -> ConditionClass | None:
    text = _opt_str(value)
    if text is None:
        return None
    for c in ConditionClass:
        if c.value.lower() == text.lower():
            return c
    return None


def _timeline(value: Any) -> IntakeTimeline | None:
    text = _opt_str(value)
    if text is None:
        return None
    for t in IntakeTimeline:
        if t.value.lower() == text.lower() and t is not IntakeTimeline.UNKNOWN:
            return t
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        text = str(v).strip()
        if text and text.lower() not in {o.lower() for o in out}:
            out.append(text)
    return out


def fields_from_parsed(parsed: Mapping[str, Any]) -> IntakeFields:
    """Model output -> ``IntakeFields`` with every value validated (unknown enums become null)."""
    allowed_unsure = {"co_owners", "occupancy", "liens", "hoa"}
    return IntakeFields(
        property_address=_opt_str(parsed.get("property_address")),
        motivation=_opt_str(parsed.get("motivation")),
        condition_class=_condition(parsed.get("condition_class")),
        disclosed_issues=_string_list(parsed.get("disclosed_issues")),
        timeline=_timeline(parsed.get("timeline")),
        timeline_text=_opt_str(parsed.get("timeline_text")),
        asking_price=_decimal(parsed.get("asking_price")),
        co_owners=_opt_str(parsed.get("co_owners")),
        occupancy=_opt_str(parsed.get("occupancy")),
        liens=_opt_str(parsed.get("liens")),
        hoa=_opt_str(parsed.get("hoa")),
        seller_unsure=[s for s in _string_list(parsed.get("seller_unsure")) if s in allowed_unsure],
        photos_mentioned=bool(parsed.get("photos_mentioned", False)),
    )


def parse_form(payload: Mapping[str, Any]) -> IntakeFields:
    """Web-form fields -> ``IntakeFields`` without a model (SEL-08 "web form -> S4")."""
    address_parts = [
        _opt_str(payload.get("address_line1")),
        _opt_str(payload.get("city")),
        " ".join(
            p for p in (_opt_str(payload.get("state")), _opt_str(payload.get("postal_code"))) if p
        )
        or None,
    ]
    address = ", ".join(p for p in address_parts if p) or None
    photos = payload.get("photos")
    return IntakeFields(
        property_address=address,
        motivation=_opt_str(payload.get("motivation")),
        condition_class=_condition(payload.get("condition_class")),
        disclosed_issues=_string_list(
            payload.get("disclosed_issues")
            if isinstance(payload.get("disclosed_issues"), list)
            else [
                s.strip()
                for s in re.split(r"[;\n,]", str(payload.get("disclosed_issues") or ""))
                if s.strip()
            ]
        ),
        timeline=_timeline(payload.get("timeline")),
        timeline_text=_opt_str(payload.get("timeline_text")),
        asking_price=_decimal(payload.get("asking_price")),
        co_owners=_opt_str(payload.get("co_owners")),
        occupancy=_opt_str(payload.get("occupancy")),
        liens=_opt_str(payload.get("liens")),
        hoa=_opt_str(payload.get("hoa")),
        photos_mentioned=bool(photos) if isinstance(photos, list | int) else False,
    )


def choose_asks(missing: Sequence[str], optional: Sequence[str]) -> list[str]:
    """SEL-07: at most two questions per message, required fields first, then one optional
    "asked" field when a slot is free."""
    asks = list(missing[:MAX_ASKS_PER_MESSAGE])
    for name in optional:
        if len(asks) >= MAX_ASKS_PER_MESSAGE:
            break
        asks.append(name)
    return asks


def photo_upload_link(public_base_url: str, seller_lead_id: uuid.UUID | str) -> str:
    return f"{public_base_url.rstrip('/')}/sell?lead={seller_lead_id}"


def assemble_reply_body(
    body: str, *, sender: SenderIdentity, photo_link: str | None, first_name: str | None = None
) -> str:
    """Model text + (photo link paragraph) + the SEL-05 compliance block."""
    text = body.strip()
    if first_name and not text.lower().startswith(("hi ", "hello ", "dear ")):
        text = f"Hi {first_name},\n\n{text}"
    if photo_link:
        text += (
            "\n\nIf you can, a few photos of the inside and outside help a lot. You can upload "
            f"them here (no account needed): {photo_link}"
        )
    return ensure_compliance_block(text, sender)


def _reply_subject(subject: str | None) -> str:
    base = (subject or "").strip() or "Your property"
    return base if base.lower().startswith("re:") else f"RE: {base}"


# --------------------------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------------------------


class S4IntakeAgent(BaseAgent):
    agent_id = AgentId.S4
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

    # -- prompts -----------------------------------------------------------------------------

    def classify_prompt(self, thread: Sequence[ThreadMessage]) -> tuple[str, bool]:
        latest = latest_inbound(thread)
        stripped = strip_quoted_text(latest.body)
        content = (
            f"{prompts.CLASSIFY_INSTRUCTIONS}\n\n"
            f"THREAD HISTORY (oldest first):\n{render_thread(thread)}\n\n"
            f"LATEST INBOUND MESSAGE from {latest.from_address or 'unknown'}"
            f" (subject: {latest.subject or '(none)'}):\n{stripped.text}"
        )
        return content, stripped.stripped

    def extract_prompt(self, thread: Sequence[ThreadMessage], known: Mapping[str, Any]) -> str:
        latest = latest_inbound(thread)
        stripped = strip_quoted_text(latest.body)
        return (
            f"{prompts.EXTRACT_INSTRUCTIONS}\n\n"
            f"ALREADY ON RECORD:\n{self.as_json(dict(known)) if known else '(nothing yet)'}\n\n"
            f"THREAD HISTORY (oldest first):\n{render_thread(thread)}\n\n"
            f"LATEST INBOUND MESSAGE:\n{stripped.text}"
        )

    def draft_prompt(
        self,
        thread: Sequence[ThreadMessage],
        *,
        known: Mapping[str, Any],
        asks: Sequence[str],
        photo_link: str | None,
        asks_offer: bool,
        facts: Mapping[str, Any] | None = None,
    ) -> str:
        latest = latest_inbound(thread)
        stripped = strip_quoted_text(latest.body)
        ask_lines = "\n".join(f"- {a}: {FIELD_QUESTIONS.get(a, a)}" for a in asks) or "(nothing)"
        return (
            f"{prompts.DRAFT_INSTRUCTIONS}\n\n"
            f"FACTS WE KNOW:\n{self.as_json(dict(facts or {}))}\n\n"
            f"INTAKE ON RECORD:\n{self.as_json(dict(known)) if known else '(nothing yet)'}\n\n"
            f"ASK FOR (at most {MAX_ASKS_PER_MESSAGE}):\n{ask_lines}\n\n"
            f"PHOTO LINK: {'will be appended' if photo_link else '(none; photos on record)'}\n\n"
            f"SELLER ASKED FOR A NUMBER: {asks_offer}\n\n"
            f"THREAD HISTORY (oldest first):\n{render_thread(thread)}\n\n"
            f"LATEST INBOUND MESSAGE from {latest.from_address or 'unknown'}"
            f" (subject: {latest.subject or '(none)'}):\n{stripped.text}"
        )

    # -- tasks -------------------------------------------------------------------------------

    async def classify(  # type: ignore[override]
        self, thread: Sequence[ThreadMessage]
    ) -> S4Classification:
        latest = latest_inbound(thread)
        content, was_stripped = self.classify_prompt(thread)
        stripped_text = strip_quoted_text(latest.body).text
        if detect_auto_reply(subject=latest.subject, body=stripped_text, headers=latest.headers):
            return S4Classification(
                classification=SellerClassification.UNRELATED,
                confidence=1.0,
                rationale="automatic reply detected from headers/subject/body",
                is_auto_reply=True,
                quoted_text_stripped=was_stripped,
            )
        request = self.build_request(
            TaskKind.CLASSIFY, content, json_schema=CLASSIFY_SCHEMA, max_tokens=400
        )
        response, run_id = await self.run(TaskKind.CLASSIFY, request)
        parsed = response.parsed
        if parsed is None:
            msg = "S4 classify returned no structured output"
            raise StructuredOutputError(msg)
        label = str(parsed.get("classification", ""))
        try:
            classification = SellerClassification(label)
        except ValueError as exc:
            msg = f"S4 classify returned unknown class {label!r}"
            raise StructuredOutputError(msg) from exc
        is_auto = bool(parsed.get("is_auto_reply", False))
        if is_auto:
            classification = SellerClassification.UNRELATED
        return S4Classification(
            classification=classification,
            confidence=min(1.0, max(0.0, float(parsed.get("confidence", 0.0)))),
            rationale=str(parsed.get("rationale", "")),
            is_auto_reply=is_auto,
            quoted_text_stripped=was_stripped,
            asks_offer=bool(parsed.get("asks_offer", False)) and not is_auto,
            run_id=run_id,
        )

    async def extract(  # type: ignore[override]
        self, thread: Sequence[ThreadMessage], known: Mapping[str, Any] | None = None
    ) -> S4Extraction:
        request = self.build_request(
            TaskKind.EXTRACT, self.extract_prompt(thread, known or {}), json_schema=EXTRACT_SCHEMA
        )
        response, run_id = await self.run(TaskKind.EXTRACT, request)
        parsed = response.parsed
        if parsed is None:
            msg = "S4 extract returned no structured output"
            raise StructuredOutputError(msg)
        return S4Extraction(
            fields=fields_from_parsed(parsed),
            rationale=str(parsed.get("rationale", "")),
            run_id=run_id,
        )

    @staticmethod
    def parse_form(payload: Mapping[str, Any]) -> S4Extraction:
        return S4Extraction(fields=parse_form(payload), rationale="web form fields", run_id=None)

    async def draft_reply(
        self,
        thread: Sequence[ThreadMessage],
        *,
        known: Mapping[str, Any],
        asks: Sequence[str],
        photo_link: str | None,
        asks_offer: bool = False,
        facts: Mapping[str, Any] | None = None,
        decision: str | None = None,
    ) -> S4ReplyDraft:
        """Draft the reply (Sonnet). ``asks_for`` is clamped to ``asks`` and to two;
        ``contains_sensitive`` is the model's flag OR the deterministic scan OR ``asks_offer``."""
        latest = latest_inbound(thread)
        content = self.draft_prompt(
            thread,
            known=known,
            asks=asks,
            photo_link=photo_link,
            asks_offer=asks_offer,
            facts=facts,
        )
        request = self.build_request(TaskKind.DRAFT, content, json_schema=DRAFT_SCHEMA)
        response, run_id = await self.run(TaskKind.DRAFT, request, decision=decision)
        parsed = response.parsed
        if parsed is None:
            msg = "S4 draft returned no structured output"
            raise StructuredOutputError(msg)
        body = str(parsed.get("body", "")).strip()
        subject = _reply_subject(str(parsed.get("subject") or "").strip() or latest.subject)
        allowed = list(asks)
        asked = [str(a) for a in parsed.get("asks_for") or [] if str(a) in allowed]
        asked = asked[:MAX_ASKS_PER_MESSAGE]
        sensitive = (
            bool(parsed.get("contains_sensitive", False))
            or contains_sensitive_content(body)
            or asks_offer
        )
        return S4ReplyDraft(
            subject=subject,
            body=body,
            asks_for=asked,
            contains_sensitive=sensitive,
            run_id=run_id,
        )


__all__ = [
    "CLASSIFY_SCHEMA",
    "DRAFT_SCHEMA",
    "EXTRACT_SCHEMA",
    "FIELD_QUESTIONS",
    "MAX_ASKS_PER_MESSAGE",
    "SENSITIVE_RE",
    "S4Classification",
    "S4Extraction",
    "S4IntakeAgent",
    "S4ReplyDraft",
    "SellerClassification",
    "assemble_reply_body",
    "choose_asks",
    "contains_sensitive_content",
    "fields_from_parsed",
    "parse_form",
    "photo_upload_link",
]
