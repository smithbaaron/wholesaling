"""``property_candidates`` and ``matches`` — D2 output (REQUIREMENTS.md BB-07..09, DM-09)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from wholesaling.db.models.enums import MatchReviewStatus, enum_column
from wholesaling.db.types import GUID, JSONDoc, Money, Ratio


class PropertyCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A listing or parcel found by D2, deduped by stable source id (BB-07)."""

    __tablename__ = "property_candidates"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_property_candidates_market_id", "market_id"),
        Index("ix_property_candidates_parcel_id", "parcel_id"),
        Index("ix_property_candidates_address_key", "address_key"),
    )

    market_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("markets.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    address_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parcel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Off-market candidates from A7 contracts (BB-12) link back to the seller lead.
    seller_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("seller_leads.id", ondelete="SET NULL"), nullable=True
    )
    list_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    est_arv: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    est_repairs: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    est_fees: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    est_assignment_spread: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    liquidity_score: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    # Title/lien/HOA/financing/data-gap flags for human due diligence (BB-08).
    flags: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)


class Match(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A candidate ranked against one search profile revision (BB-09)."""

    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("search_profile_id", "property_candidate_id"),
        Index("ix_matches_search_profile_id", "search_profile_id"),
        Index("ix_matches_property_candidate_id", "property_candidate_id"),
        Index("ix_matches_review_status", "review_status"),
    )

    search_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("search_profiles.id", ondelete="CASCADE"), nullable=False
    )
    property_candidate_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("property_candidates.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    # Reason for match, match logic, missing fields, freshness, uncertainty, next action (BB-08).
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    review_status: Mapped[MatchReviewStatus] = mapped_column(
        enum_column(MatchReviewStatus, "match_review_status"),
        nullable=False,
        default=MatchReviewStatus.PENDING,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )
