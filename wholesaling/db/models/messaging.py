"""``threads``, ``messages``, ``sequences``, ``suppression``.

REQUIREMENTS.md INV-10..16, WF-02/03, WF-21.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from wholesaling.db.models.enums import (
    Channel,
    Direction,
    MessageClassification,
    PolicyDecision,
    SequenceKind,
    SequenceStatus,
    enum_column,
)
from wholesaling.db.types import GUID, JSONDoc


class Thread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One conversation (Graph ``conversationId`` for Outlook; INV-23 "one Outlook conversation").

    For a Graph thread ``external_thread_id`` is the ``conversationId``.
    """

    __tablename__ = "threads"
    __table_args__ = (
        UniqueConstraint("channel", "external_thread_id"),
        Index("ix_threads_investor_id", "investor_id"),
        Index("ix_threads_seller_lead_id", "seller_lead_id"),
    )

    channel: Mapped[Channel] = mapped_column(enum_column(Channel, "channel"), nullable=False)
    external_thread_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    investor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("investors.id", ondelete="SET NULL"), nullable=True
    )
    seller_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("seller_leads.id", ondelete="SET NULL"), nullable=True
    )
    counterpart_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Every inbound and outbound message, drafts included.

    ``(channel, external_message_id)`` is the inbound idempotency key (WF-19 "dedupe by message
    ID"; INV-24 "one inbound test message produces exactly one logged event").
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("channel", "external_message_id"),
        Index("ix_messages_thread_id", "thread_id"),
        Index("ix_messages_direction_draft", "direction", "draft"),
        Index("ix_messages_classification", "classification"),
        Index("ix_messages_sequence_id", "sequence_id"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[Direction] = mapped_column(
        enum_column(Direction, "direction", length=3), nullable=False
    )
    channel: Mapped[Channel] = mapped_column(enum_column(Channel, "channel"), nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    in_reply_to_external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    from_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    to_addresses: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[MessageClassification | None] = mapped_column(
        enum_column(MessageClassification, "message_classification"), nullable=True
    )
    classification_confidence: Mapped[float | None] = mapped_column(nullable=True)
    draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policy_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_decision: Mapped[PolicyDecision | None] = mapped_column(
        enum_column(PolicyDecision, "policy_decision"), nullable=True
    )
    # Points at ``qa_reviews.id``. Not a DB-level FK: ``qa_reviews.message_id`` already references
    # this table and a two-way FK cycle is not portable to SQLite batch migrations (ADR 0002).
    qa_review_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    sequence_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("sequences.id", ondelete="SET NULL"), nullable=True
    )
    sequence_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    meta: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)


class Sequence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A durable outreach sequence; the send timers live in the Temporal workflow (DM-07)."""

    __tablename__ = "sequences"
    __table_args__ = (
        UniqueConstraint("temporal_workflow_id"),
        CheckConstraint(
            "(kind = 'investor' AND investor_id IS NOT NULL AND seller_lead_id IS NULL) OR "
            "(kind = 'seller' AND seller_lead_id IS NOT NULL AND investor_id IS NULL)",
            name="target_matches_kind",
        ),
        Index("ix_sequences_investor_id", "investor_id"),
        Index("ix_sequences_seller_lead_id", "seller_lead_id"),
        Index("ix_sequences_status", "status"),
    )

    kind: Mapped[SequenceKind] = mapped_column(
        enum_column(SequenceKind, "sequence_kind"), nullable=False
    )
    investor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("investors.id", ondelete="CASCADE"), nullable=True
    )
    seller_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("seller_leads.id", ondelete="CASCADE"), nullable=True
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("threads.id", ondelete="SET NULL"), nullable=True
    )
    temporal_workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[SequenceStatus] = mapped_column(
        enum_column(SequenceStatus, "sequence_status"),
        nullable=False,
        default=SequenceStatus.ACTIVE,
    )
    step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Suppression(UUIDPrimaryKeyMixin, Base):
    """Opt-outs, bounces and DNC entries (INV-15, WF-21, DM-08).

    Append-only; rows are never removed.
    """

    __tablename__ = "suppression"
    __table_args__ = (UniqueConstraint("channel", "normalized_value"),)

    channel: Mapped[Channel] = mapped_column(enum_column(Channel, "channel"), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
