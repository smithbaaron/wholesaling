"""Enumerations used by the schema.

Stored as portable VARCHAR columns with a CHECK constraint (``native_enum=False``), never as a
Postgres ``ENUM`` type (docs/adr/0002-portable-schema.md). Python code uses the ``StrEnum`` values.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum

from wholesaling.policy.actions import AutonomyLevel, PolicyAction

__all__ = [
    "Actor",
    "AgentId",
    "ArvConfidence",
    "AutonomyLevel",
    "Channel",
    "ConditionClass",
    "ContactChannel",
    "Direction",
    "IntakeTimeline",
    "InvestorType",
    "MarketTier",
    "MatchReviewStatus",
    "MessageClassification",
    "OfferStatus",
    "PolicyAction",
    "PolicyDecision",
    "QaVerdict",
    "SearchProfileStatus",
    "SellerLeadStatus",
    "SequenceKind",
    "SequenceStatus",
    "VerificationPass",
    "VerificationResult",
    "enum_column",
]


class MarketTier(StrEnum):
    """M1-04."""

    ACTIVE = "Active"
    WATCH = "Watch"
    EXCLUDED = "Excluded"


class InvestorType(StrEnum):
    """INV-03."""

    FLIPPER = "flipper"
    BUY_AND_HOLD = "buy_and_hold"
    WHOLESALER = "wholesaler"
    IBUYER_INSTITUTIONAL = "ibuyer_institutional"
    AGENT_INVESTOR = "agent_investor"
    UNKNOWN = "unknown"


class ContactChannel(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"


class Channel(StrEnum):
    """Message / thread channel. SMS is modelled but never sends in this build (RUL-06)."""

    EMAIL = "email"
    SMS = "sms"
    WEB_FORM = "web_form"


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


class MessageClassification(StrEnum):
    """A4 classification list (INV-13)."""

    INTERESTED = "interested"
    BUY_BOX_SUPPLIED = "buy_box_supplied"
    QUESTION = "question"
    NOT_NOW = "not_now"
    NOT_INTERESTED = "not_interested"
    OPT_OUT = "opt_out"
    AMBIGUOUS = "ambiguous"
    UNRELATED = "unrelated"


class PolicyDecision(StrEnum):
    SEND = "send"
    QUEUE_FOR_HUMAN = "queue_for_human"
    BLOCKED = "blocked"


class SearchProfileStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INACTIVE = "inactive"


class SellerLeadStatus(StrEnum):
    NEW = "new"
    QUEUED = "queued"
    IN_SEQUENCE = "in_sequence"
    REPLIED = "replied"
    INTAKE = "intake"
    OFFERED = "offered"
    UNDER_CONTRACT = "under_contract"
    CLOSED = "closed"
    DEAD = "dead"
    SUPPRESSED = "suppressed"


class ConditionClass(StrEnum):
    """SEL-06."""

    LIGHT = "Light"
    MEDIUM = "Medium"
    HEAVY = "Heavy"
    GUT = "Gut"


class IntakeTimeline(StrEnum):
    ASAP = "asap"
    WITHIN_30_DAYS = "within_30_days"
    WITHIN_90_DAYS = "within_90_days"
    FLEXIBLE = "flexible"
    UNKNOWN = "unknown"


class ArvConfidence(StrEnum):
    """A7-04."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class OfferStatus(StrEnum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    BLOCKED = "blocked"
    SENT = "sent"
    COUNTERED = "countered"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class VerificationPass(StrEnum):
    """A7-12..17."""

    V1_DUAL_SOURCE_FACTS = "V1"
    V2_DUAL_METHOD_ARV = "V2"
    V3_COMP_AUDIT = "V3"
    V4_REPAIR_CROSS_CHECK = "V4"
    V5_INDEPENDENT_RECOMPUTE = "V5"
    V6_PRE_SEND_DRIFT = "V6"


class VerificationResult(StrEnum):
    PASS = "pass"  # noqa: S105 - verification outcome, not a credential
    FAIL = "fail"
    ADJUSTED = "adjusted"
    ESCALATE = "escalate"


class MatchReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SequenceKind(StrEnum):
    INVESTOR = "investor"
    SELLER = "seller"


class SequenceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class QaVerdict(StrEnum):
    """INV-17."""

    APPROVE = "approve"
    BLOCK = "block"
    REWRITE = "rewrite"


class AgentId(StrEnum):
    """Agent roster (REQUIREMENTS.md section 5)."""

    M1 = "M1"
    A1 = "A1"
    A2 = "A2"
    A3 = "A3"
    A4 = "A4"
    A5 = "A5"
    A6 = "A6"
    A7 = "A7"
    D1 = "D1"
    D2 = "D2"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"


class Actor(StrEnum):
    SYSTEM = "system"
    HUMAN = "human"
    AGENT = "agent"


def enum_column(enum_cls: type[StrEnum], name: str, length: int = 32) -> Enum:
    """A portable enum column: VARCHAR(length) plus a named CHECK constraint.

    A new ``Enum`` instance is returned per call so each table gets its own constraint. The DB
    stores the enum *values* (``"Active"``), not the member names.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda cls: [m.value for m in cls],
        validate_strings=True,
    )
