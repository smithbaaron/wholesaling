"""``policies``, ``qa_reviews``, ``agent_runs``, ``audit_events``.

REQUIREMENTS.md POL-01, INV-18, DM-10.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from wholesaling.db.models.enums import (
    Actor,
    AgentId,
    AutonomyLevel,
    PolicyAction,
    QaVerdict,
    enum_column,
)
from wholesaling.db.types import GUID, JSONDoc, Money, Ratio


class Policy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Live autonomy level per action (POL-01).

    Behaviour per level is ``policy.actions.POLICY_TABLE``.

    Migration 0001 seeds all twelve actions at L0 (POL-02).
    """

    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("action"),)

    action: Mapped[PolicyAction] = mapped_column(
        enum_column(PolicyAction, "policy_action", length=40), nullable=False
    )
    level: Mapped[AutonomyLevel] = mapped_column(
        enum_column(AutonomyLevel, "autonomy_level", length=4),
        nullable=False,
        default=AutonomyLevel.L0,
    )
    daily_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One model invocation by one agent, with cost (RUL-04 budget tracking)."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_agent", "agent"),
        Index("ix_agent_runs_correlation_id", "correlation_id"),
    )

    agent: Mapped[AgentId] = mapped_column(
        enum_column(AgentId, "agent_id", length=4), nullable=False
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    output: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class QaReview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A5 verdict on an outbound draft or offer (INV-17, INV-18)."""

    __tablename__ = "qa_reviews"
    __table_args__ = (
        Index("ix_qa_reviews_message_id", "message_id"),
        Index("ix_qa_reviews_offer_id", "offer_id"),
        Index("ix_qa_reviews_verdict", "verdict"),
    )

    message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
    offer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("offers.id", ondelete="CASCADE"), nullable=True
    )
    verdict: Mapped[QaVerdict] = mapped_column(enum_column(QaVerdict, "qa_verdict"), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    flags: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    rewrite_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only audit log. ``idempotency_key`` makes replayed activities record one event."""

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_event_type", "event_type"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    actor: Mapped[Actor] = mapped_column(enum_column(Actor, "actor", length=8), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
