"""``seller_leads`` and ``intakes`` (REQUIREMENTS.md SEL-02, SEL-06, SEL-07)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from wholesaling.db.models.enums import (
    ConditionClass,
    IntakeTimeline,
    SellerLeadStatus,
    enum_column,
)
from wholesaling.db.types import GUID, JSONDoc, Money


class SellerLead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "seller_leads"
    __table_args__ = (
        Index("ix_seller_leads_market_id", "market_id"),
        Index("ix_seller_leads_status", "status"),
        Index("ix_seller_leads_parcel_id", "parcel_id"),
        Index("ix_seller_leads_address_key", "address_key"),
    )

    market_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("markets.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[SellerLeadStatus] = mapped_column(
        enum_column(SellerLeadStatus, "seller_lead_status"),
        nullable=False,
        default=SellerLeadStatus.NEW,
    )
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    property_address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    property_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    property_state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    property_postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # ``normalize.normalize_address`` output; dedupe key alongside parcel_id.
    address_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parcel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parcel_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # S1 distress signals present, e.g. ["absentee", "tax_delinquent"] (SEL-02).
    signals: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    signal_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assessed_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    estimated_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_refs: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    # Skip-trace output lives here with provenance (SEL-03); investors use ``contacts``.
    contact_channels: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONDoc(), nullable=False, default=list
    )
    web_form_submission_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_contacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Intake(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """S4 conversational intake (SEL-06). Unknowns are flagged, never guessed."""

    __tablename__ = "intakes"
    __table_args__ = (
        Index("ix_intakes_seller_lead_id", "seller_lead_id"),
        Index("ix_intakes_intake_complete", "intake_complete"),
    )

    seller_lead_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("seller_leads.id", ondelete="CASCADE"), nullable=False
    )
    motivation: Mapped[str | None] = mapped_column(Text, nullable=True)
    condition_class: Mapped[ConditionClass | None] = mapped_column(
        enum_column(ConditionClass, "condition_class"), nullable=True
    )
    disclosed_issues: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    timeline: Mapped[IntakeTimeline | None] = mapped_column(
        enum_column(IntakeTimeline, "intake_timeline"), nullable=True
    )
    timeline_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    asking_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # [{"blob_key": ..., "uploaded_at": ..., "caption": ...}]
    photos: Mapped[list[dict[str, Any]]] = mapped_column(JSONDoc(), nullable=False, default=list)
    photo_link_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    co_owners: Mapped[str | None] = mapped_column(Text, nullable=True)
    co_owners_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    occupancy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occupancy_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    liens: Mapped[str | None] = mapped_column(Text, nullable=True)
    liens_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hoa: Mapped[str | None] = mapped_column(String(255), nullable=True)
    hoa_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Beds/baths/sqft/year/lot as resolved by A7 step 1 (county + provider), with sources.
    property_facts: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    missing_required: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    intake_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
