"""Pure functions turning ORM rows into sheet rows (REQUIREMENTS.md INT-14, STK-02).

``TAB_SPECS`` is the contract: tab name, header row (defined once, here), key column. Every
row starts with the stable Postgres id of its entity (``investor_id``, ``buy_box_id``, ...),
datetimes are ISO-8601 UTC strings, money and ratios are numbers, lists are ``"; "``-joined,
nested JSON is compact JSON text. Every tab carries a ``status`` column because the projection
never deletes: a deactivated buy box or an excluded investor keeps its row and changes status.

Nothing in this module touches the database or the Sheets API. ``activities/sheets.py`` loads
a ``ProjectionSnapshot`` from Postgres and calls ``build_rows``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wholesaling.db.models.buy_boxes import BuyBox, SearchProfile
from wholesaling.db.models.deals import Match, PropertyCandidate
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.messaging import Message, Suppression, Thread
from wholesaling.db.models.ops import QaReview
from wholesaling.integrations.sheets.interface import Cell, SheetRow, TabSpec, iso_utc

TAB_INVESTORS = "Investors"
TAB_CONTACTS = "Contacts"
TAB_BUY_BOXES = "Buy Boxes"
TAB_SEARCH_PROFILES = "Search Profiles"
TAB_PROPERTIES = "Properties"
TAB_MATCHES = "Matches"
TAB_CONVERSATIONS = "Conversations"
TAB_SUPPRESSIONS = "Suppressions"
TAB_QA_LOG = "QA Log"
TAB_OUTREACH_EVENTS = "Outreach Events"

TAB_SPECS: dict[str, TabSpec] = {
    TAB_INVESTORS: TabSpec(
        name=TAB_INVESTORS,
        header=(
            "investor_id",
            "status",
            "canonical_name",
            "first_name",
            "investor_type",
            "score",
            "market_id",
            "market_name",
            "is_entity",
            "mailing_address_line1",
            "mailing_city",
            "mailing_state",
            "mailing_postal_code",
            "purchase_count_24m",
            "last_purchase_at",
            "excluded_reason",
            "aliases",
            "created_at",
            "updated_at",
        ),
        requirement="INT-14",
    ),
    TAB_CONTACTS: TabSpec(
        name=TAB_CONTACTS,
        header=(
            "contact_id",
            "investor_id",
            "status",
            "channel",
            "value",
            "normalized_value",
            "is_primary",
            "provenance",
            "provenance_url",
            "use_limitations",
            "confidence",
            "verified_at",
            "created_at",
            "updated_at",
        ),
        requirement="INT-14",
    ),
    TAB_BUY_BOXES: TabSpec(
        name=TAB_BUY_BOXES,
        header=(
            "buy_box_id",
            "investor_id",
            "buy_box_key",
            "version",
            "status",
            "is_current",
            "is_active",
            "markets",
            "cities",
            "zips",
            "neighborhoods",
            "asset_types",
            "price_min",
            "price_max",
            "condition",
            "strategy",
            "financing",
            "closing_preferences",
            "other_criteria",
            "unknown_fields",
            "raw_text",
            "source_message_id",
            "source_ref",
            "change_note",
            "created_at",
            "updated_at",
        ),
        requirement="INT-14 / INV-20",
    ),
    TAB_SEARCH_PROFILES: TabSpec(
        name=TAB_SEARCH_PROFILES,
        header=(
            "search_profile_id",
            "buy_box_id",
            "investor_id",
            "version",
            "status",
            "criteria",
            "unknowns",
            "criteria_hash",
            "created_at",
            "updated_at",
        ),
        requirement="INT-14 / DM-05",
    ),
    TAB_PROPERTIES: TabSpec(
        name=TAB_PROPERTIES,
        header=(
            "property_candidate_id",
            "status",
            "source",
            "external_id",
            "market_id",
            "address_line1",
            "city",
            "state",
            "postal_code",
            "parcel_id",
            "seller_lead_id",
            "list_price",
            "est_arv",
            "est_repairs",
            "est_fees",
            "est_assignment_spread",
            "liquidity_score",
            "flags",
            "source_url",
            "retrieved_at",
            "created_at",
            "updated_at",
        ),
        requirement="INT-14",
    ),
    TAB_MATCHES: TabSpec(
        name=TAB_MATCHES,
        header=(
            "match_id",
            "search_profile_id",
            "property_candidate_id",
            "buy_box_id",
            "investor_id",
            "status",
            "score",
            "confidence",
            "reviewed_by",
            "reviewed_at",
            "reason",
            "next_action",
            "created_at",
            "updated_at",
        ),
        requirement="INT-14",
    ),
    TAB_CONVERSATIONS: TabSpec(
        name=TAB_CONVERSATIONS,
        header=(
            "thread_id",
            "status",
            "channel",
            "external_thread_id",
            "investor_id",
            "seller_lead_id",
            "subject",
            "counterpart_address",
            "message_count",
            "inbound_count",
            "outbound_count",
            "last_direction",
            "last_classification",
            "last_message_at",
            "created_at",
            "updated_at",
        ),
        requirement="INT-14",
    ),
    TAB_SUPPRESSIONS: TabSpec(
        name=TAB_SUPPRESSIONS,
        header=(
            "suppression_id",
            "status",
            "channel",
            "normalized_value",
            "reason",
            "source_message_id",
            "created_at",
        ),
        requirement="INT-14",
    ),
    TAB_QA_LOG: TabSpec(
        name=TAB_QA_LOG,
        header=(
            "qa_review_id",
            "message_id",
            "offer_id",
            "status",
            "verdict",
            "confidence",
            "flags",
            "rationale",
            "model",
            "agent_run_id",
            "created_at",
        ),
        requirement="INT-14",
    ),
    TAB_OUTREACH_EVENTS: TabSpec(
        name=TAB_OUTREACH_EVENTS,
        header=(
            "message_id",
            "thread_id",
            "investor_id",
            "status",
            "direction",
            "channel",
            "sequence_id",
            "sequence_step",
            "from_address",
            "to_addresses",
            "subject",
            "classification",
            "policy_decision",
            "sent_at",
            "received_at",
            "created_at",
        ),
        requirement="INT-14 (MS Outreach Events)",
    ),
}

TAB_ORDER: tuple[str, ...] = tuple(TAB_SPECS)

# Investor statuses
STATUS_ACTIVE = "active"
STATUS_EXCLUDED = "excluded"
# Buy box statuses
STATUS_CURRENT = "current"
STATUS_SUPERSEDED = "superseded"
STATUS_INACTIVE = "inactive"
# Message statuses
STATUS_DRAFT = "draft"
STATUS_SENT = "sent"
STATUS_RECEIVED = "received"
STATUS_SUPPRESSED = "suppressed"


# --------------------------------------------------------------------------------------------
# Cell helpers
# --------------------------------------------------------------------------------------------


def _id(value: uuid.UUID | str | None) -> str | None:
    return None if value is None else str(value)


def _num(value: Decimal | int | float | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _enum(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _joined(values: Iterable[Any] | None) -> str | None:
    if not values:
        return None
    return "; ".join(str(v) for v in values)


def _json(value: Mapping[str, Any] | Sequence[Any] | None) -> str | None:
    if not value:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _text(value: str | None, limit: int = 5000) -> str | None:
    """Sheets caps a cell at 50,000 characters; we keep raw text readable at 5,000."""
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


# --------------------------------------------------------------------------------------------
# Snapshot and per-tab projections
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ConversationStats:
    message_count: int = 0
    inbound_count: int = 0
    outbound_count: int = 0
    last_direction: str | None = None
    last_classification: str | None = None


@dataclass(slots=True)
class ProjectionSnapshot:
    """Everything ``build_rows`` needs, loaded in one pass by the sync activity."""

    investors: list[Investor] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    buy_boxes: list[BuyBox] = field(default_factory=list)
    search_profiles: list[SearchProfile] = field(default_factory=list)
    properties: list[PropertyCandidate] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    threads: list[Thread] = field(default_factory=list)
    suppressions: list[Suppression] = field(default_factory=list)
    qa_reviews: list[QaReview] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    # Lookups the joins need: market id -> name; buy box id -> investor id;
    # search profile id -> buy box id; thread id -> stats.
    market_names: dict[uuid.UUID, str] = field(default_factory=dict)
    buy_box_investor: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    profile_buy_box: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    thread_investor: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)
    conversation_stats: dict[uuid.UUID, ConversationStats] = field(default_factory=dict)


def investor_status(row: Investor) -> str:
    return STATUS_EXCLUDED if row.excluded_reason else STATUS_ACTIVE


def project_investor(row: Investor, *, market_name: str | None = None) -> SheetRow:
    return [
        str(row.id),
        investor_status(row),
        row.canonical_name,
        row.first_name,
        _enum(row.investor_type),
        row.score,
        _id(row.market_id),
        market_name,
        bool(row.is_entity),
        row.mailing_address_line1,
        row.mailing_city,
        row.mailing_state,
        row.mailing_postal_code,
        row.purchase_count_24m,
        iso_utc(row.last_purchase_at),
        row.excluded_reason,
        _joined(row.aliases),
        iso_utc(row.created_at),
        iso_utc(row.updated_at),
    ]


def project_contact(row: Contact) -> SheetRow:
    return [
        str(row.id),
        str(row.investor_id),
        STATUS_ACTIVE,
        _enum(row.channel),
        row.value,
        row.normalized_value,
        bool(row.is_primary),
        row.provenance,
        row.provenance_url,
        row.use_limitations,
        _num(row.confidence),
        iso_utc(row.verified_at),
        iso_utc(row.created_at),
        iso_utc(row.updated_at),
    ]


def buy_box_status(row: BuyBox) -> str:
    if not row.is_active:
        return STATUS_INACTIVE
    return STATUS_CURRENT if row.is_current else STATUS_SUPERSEDED


def project_buy_box(row: BuyBox) -> SheetRow:
    geo = row.geography or {}
    return [
        str(row.id),
        str(row.investor_id),
        row.buy_box_key,
        row.version,
        buy_box_status(row),
        bool(row.is_current),
        bool(row.is_active),
        _joined(geo.get("markets")),
        _joined(geo.get("cities")),
        _joined(geo.get("zips")),
        _joined(geo.get("neighborhoods")),
        _joined(row.asset_types),
        _num(row.price_min),
        _num(row.price_max),
        row.condition,
        row.strategy,
        row.financing,
        _json(row.closing_preferences),
        _json(row.other_criteria),
        _joined(row.unknown_fields),
        _text(row.raw_text),
        _id(row.source_message_id),
        row.source_ref,
        row.change_note,
        iso_utc(row.created_at),
        iso_utc(row.updated_at),
    ]


def project_search_profile(row: SearchProfile, *, investor_id: uuid.UUID | None) -> SheetRow:
    return [
        str(row.id),
        str(row.buy_box_id),
        _id(investor_id),
        row.version,
        _enum(row.status),
        _json(row.criteria),
        _joined(row.unknowns),
        row.criteria_hash,
        iso_utc(row.created_at),
        iso_utc(row.updated_at),
    ]


def project_property(row: PropertyCandidate) -> SheetRow:
    return [
        str(row.id),
        STATUS_ACTIVE,
        row.source,
        row.external_id,
        _id(row.market_id),
        row.address_line1,
        row.city,
        row.state,
        row.postal_code,
        row.parcel_id,
        _id(row.seller_lead_id),
        _num(row.list_price),
        _num(row.est_arv),
        _num(row.est_repairs),
        _num(row.est_fees),
        _num(row.est_assignment_spread),
        _num(row.liquidity_score),
        _joined(row.flags),
        row.source_url,
        iso_utc(row.retrieved_at),
        iso_utc(row.created_at),
        iso_utc(row.updated_at),
    ]


def project_match(
    row: Match, *, buy_box_id: uuid.UUID | None, investor_id: uuid.UUID | None
) -> SheetRow:
    evidence = row.evidence or {}
    return [
        str(row.id),
        str(row.search_profile_id),
        str(row.property_candidate_id),
        _id(buy_box_id),
        _id(investor_id),
        _enum(row.review_status),
        _num(row.score),
        _num(row.confidence),
        row.reviewed_by,
        iso_utc(row.reviewed_at),
        _text(str(evidence["reason"]) if evidence.get("reason") is not None else None, 1000),
        _text(
            str(evidence["next_action"]) if evidence.get("next_action") is not None else None, 500
        ),
        iso_utc(row.created_at),
        iso_utc(row.updated_at),
    ]


def project_conversation(row: Thread, *, stats: ConversationStats | None = None) -> SheetRow:
    s = stats or ConversationStats()
    return [
        str(row.id),
        STATUS_ACTIVE,
        _enum(row.channel),
        row.external_thread_id,
        _id(row.investor_id),
        _id(row.seller_lead_id),
        row.subject,
        row.counterpart_address,
        s.message_count,
        s.inbound_count,
        s.outbound_count,
        s.last_direction,
        s.last_classification,
        iso_utc(row.last_message_at),
        iso_utc(row.created_at),
        iso_utc(row.updated_at),
    ]


def project_suppression(row: Suppression) -> SheetRow:
    return [
        str(row.id),
        STATUS_SUPPRESSED,
        _enum(row.channel),
        row.normalized_value,
        row.reason,
        _id(row.source_message_id),
        iso_utc(row.created_at),
    ]


def project_qa_review(row: QaReview) -> SheetRow:
    verdict = _enum(row.verdict)
    return [
        str(row.id),
        _id(row.message_id),
        _id(row.offer_id),
        verdict,
        verdict,
        _num(row.confidence),
        _joined(row.flags),
        _text(row.rationale, 2000),
        row.model,
        _id(row.agent_run_id),
        iso_utc(row.created_at),
    ]


def message_status(row: Message) -> str:
    if _enum(row.direction) == "in":
        return STATUS_RECEIVED
    return STATUS_SENT if row.sent_at is not None else STATUS_DRAFT


def project_outreach_event(row: Message, *, investor_id: uuid.UUID | None) -> SheetRow:
    return [
        str(row.id),
        str(row.thread_id),
        _id(investor_id),
        message_status(row),
        _enum(row.direction),
        _enum(row.channel),
        _id(row.sequence_id),
        row.sequence_step,
        row.from_address,
        _joined(row.to_addresses),
        row.subject,
        _enum(row.classification),
        _enum(row.policy_decision),
        iso_utc(row.sent_at),
        iso_utc(row.received_at),
        iso_utc(row.created_at),
    ]


def build_rows(tab: str, snapshot: ProjectionSnapshot) -> list[SheetRow]:
    """Rows for one tab, in a stable order (by id) so repeated syncs write identical batches."""
    if tab == TAB_INVESTORS:
        rows = [
            project_investor(
                i,
                market_name=(
                    snapshot.market_names.get(i.market_id) if i.market_id is not None else None
                ),
            )
            for i in snapshot.investors
        ]
    elif tab == TAB_CONTACTS:
        rows = [project_contact(c) for c in snapshot.contacts]
    elif tab == TAB_BUY_BOXES:
        rows = [project_buy_box(b) for b in snapshot.buy_boxes]
    elif tab == TAB_SEARCH_PROFILES:
        rows = [
            project_search_profile(p, investor_id=snapshot.buy_box_investor.get(p.buy_box_id))
            for p in snapshot.search_profiles
        ]
    elif tab == TAB_PROPERTIES:
        rows = [project_property(p) for p in snapshot.properties]
    elif tab == TAB_MATCHES:
        rows = []
        for m in snapshot.matches:
            bb = snapshot.profile_buy_box.get(m.search_profile_id)
            inv = snapshot.buy_box_investor.get(bb) if bb is not None else None
            rows.append(project_match(m, buy_box_id=bb, investor_id=inv))
    elif tab == TAB_CONVERSATIONS:
        rows = [
            project_conversation(t, stats=snapshot.conversation_stats.get(t.id))
            for t in snapshot.threads
        ]
    elif tab == TAB_SUPPRESSIONS:
        rows = [project_suppression(s) for s in snapshot.suppressions]
    elif tab == TAB_QA_LOG:
        rows = [project_qa_review(q) for q in snapshot.qa_reviews]
    elif tab == TAB_OUTREACH_EVENTS:
        rows = [
            project_outreach_event(m, investor_id=snapshot.thread_investor.get(m.thread_id))
            for m in snapshot.messages
        ]
    else:
        msg = f"unknown tab {tab!r}; known: {', '.join(TAB_ORDER)}"
        raise KeyError(msg)
    spec = TAB_SPECS[tab]
    for row in rows:
        if len(row) != spec.width:  # pragma: no cover - guarded by the header-contract tests
            msg = f"{tab}: row width {len(row)} != header width {spec.width}"
            raise ValueError(msg)
    rows.sort(key=lambda r: str(r[spec.key_col]))
    return rows


def row_as_dict(tab: str, row: Sequence[Cell]) -> dict[str, Cell]:
    """``{header: cell}`` for one row (tests, the reconciliation report, the pilot report)."""
    header = TAB_SPECS[tab].header
    return {name: (row[i] if i < len(row) else None) for i, name in enumerate(header)}


__all__ = [
    "STATUS_ACTIVE",
    "STATUS_CURRENT",
    "STATUS_DRAFT",
    "STATUS_EXCLUDED",
    "STATUS_INACTIVE",
    "STATUS_RECEIVED",
    "STATUS_SENT",
    "STATUS_SUPERSEDED",
    "STATUS_SUPPRESSED",
    "TAB_BUY_BOXES",
    "TAB_CONTACTS",
    "TAB_CONVERSATIONS",
    "TAB_INVESTORS",
    "TAB_MATCHES",
    "TAB_ORDER",
    "TAB_OUTREACH_EVENTS",
    "TAB_PROPERTIES",
    "TAB_QA_LOG",
    "TAB_SEARCH_PROFILES",
    "TAB_SPECS",
    "TAB_SUPPRESSIONS",
    "ConversationStats",
    "ProjectionSnapshot",
    "build_rows",
    "buy_box_status",
    "investor_status",
    "message_status",
    "project_buy_box",
    "project_contact",
    "project_conversation",
    "project_investor",
    "project_match",
    "project_outreach_event",
    "project_property",
    "project_qa_review",
    "project_search_profile",
    "project_suppression",
    "row_as_dict",
]
