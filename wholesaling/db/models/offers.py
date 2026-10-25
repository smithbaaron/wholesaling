"""``offers`` and ``offer_verifications`` (REQUIREMENTS.md section 11, A7-18)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from wholesaling.db.models.enums import (
    ArvConfidence,
    AutonomyLevel,
    OfferStatus,
    VerificationPass,
    VerificationResult,
    enum_column,
)
from wholesaling.db.types import GUID, JSONDoc, Money, Ratio


class Offer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One A7 offer with its full calculation record (A7-01 "the full calculation is logged")."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        Index("ix_offers_seller_lead_id", "seller_lead_id"),
        Index("ix_offers_intake_id", "intake_id"),
        Index("ix_offers_status", "status"),
        Index("ix_offers_parcel_id", "parcel_id"),
    )

    seller_lead_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("seller_leads.id", ondelete="CASCADE"), nullable=False
    )
    intake_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("intakes.id", ondelete="RESTRICT"), nullable=False
    )
    # Denormalised for the "no active offer on same parcel" gate (A7-08).
    parcel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[OfferStatus] = mapped_column(
        enum_column(OfferStatus, "offer_status"), nullable=False, default=OfferStatus.DRAFT
    )
    arv: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    arv_confidence: Mapped[ArvConfidence | None] = mapped_column(
        enum_column(ArvConfidence, "arv_confidence"), nullable=True
    )
    arv_avm: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    comp_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repairs: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    market_pct: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    assignment_fee: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    closing_costs: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    mao: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    negotiation_room_pct: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    offer_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # Step 7 gate results, one entry per gate: {"name": ..., "passed": bool, "detail": ...}
    gates: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc(), nullable=False, default=list)
    open_flags: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    calc_record: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    draft_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_level_at_send: Mapped[AutonomyLevel | None] = mapped_column(
        enum_column(AutonomyLevel, "autonomy_level", length=4), nullable=True
    )
    sent_message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Workflow-supplied key so a retried OfferWorkflow activity cannot create a second offer.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)


class OfferVerification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One iteration of one verification pass V1..V6 (A7-12..18)."""

    __tablename__ = "offer_verifications"
    __table_args__ = (
        UniqueConstraint("offer_id", "pass_id", "iteration"),
        Index("ix_offer_verifications_offer_id", "offer_id"),
    )

    offer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    # ``pass`` is a Python keyword; the column is still named ``pass_id``.
    pass_id: Mapped[VerificationPass] = mapped_column(
        enum_column(VerificationPass, "verification_pass", length=4), nullable=False
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    result: Mapped[VerificationResult] = mapped_column(
        enum_column(VerificationResult, "verification_result"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
