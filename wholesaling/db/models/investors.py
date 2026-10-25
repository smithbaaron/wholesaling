"""``investors``, ``contacts``, ``source_evidence`` (REQUIREMENTS.md INV-02..08, DM-02, DM-06)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from wholesaling.db.models.enums import ContactChannel, InvestorType, enum_column
from wholesaling.db.types import GUID, JSONDoc, Ratio


class Investor(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One resolved investor entity.

    Entity resolution (INV-06) collapses LLC name variants onto ``entity_key``
    (``normalize.normalize_entity_name``). Every source record that contributed is kept as a
    ``source_evidence`` row; merges never delete evidence.
    """

    __tablename__ = "investors"
    __table_args__ = (
        UniqueConstraint("entity_key"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="score_range"),
        Index("ix_investors_market_id", "market_id"),
        Index("ix_investors_investor_type", "investor_type"),
        Index("ix_investors_score", "score"),
    )

    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Raw name spellings seen in sources, kept for display and audit.
    aliases: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    investor_type: Mapped[InvestorType] = mapped_column(
        enum_column(InvestorType, "investor_type"),
        nullable=False,
        default=InvestorType.UNKNOWN,
    )
    is_entity: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # A1 score 0..100 (INV-04). Null until scored.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    market_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("markets.id", ondelete="SET NULL"), nullable=True
    )
    # Mailing address from deed records (feeds A2; also an entity-resolution signal).
    mailing_address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mailing_address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mailing_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    mailing_state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    mailing_postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mailing_address_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    purchase_count_24m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_purchase_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Source identifiers (deed record ids, provider ids) as {source: [ref, ...]}.
    source_refs: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    excluded_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    contacts: Mapped[list[Contact]] = relationship(back_populates="investor")
    evidence: Mapped[list[SourceEvidence]] = relationship(back_populates="investor")


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A2 contact channel with provenance (INV-08, INV-09, DM-02)."""

    __tablename__ = "contacts"
    __table_args__ = (
        # DM-02 "unique email normalization", generalised to every channel.
        UniqueConstraint("channel", "normalized_value"),
        Index("ix_contacts_investor_id", "investor_id"),
    )

    investor_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("investors.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[ContactChannel] = mapped_column(
        enum_column(ContactChannel, "contact_channel"), nullable=False
    )
    value: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(512), nullable=False)
    provenance: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provenance_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    use_limitations: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    investor: Mapped[Investor] = relationship(back_populates="contacts")


class SourceEvidence(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Every source record behind an investor or seller lead (INV-06 "kept as evidence")."""

    __tablename__ = "source_evidence"
    __table_args__ = (
        CheckConstraint(
            "investor_id IS NOT NULL OR seller_lead_id IS NOT NULL", name="has_subject"
        ),
        Index("ix_source_evidence_investor_id", "investor_id"),
        Index("ix_source_evidence_seller_lead_id", "seller_lead_id"),
        Index("ix_source_evidence_source_ref", "source", "ref"),
    )

    investor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("investors.id", ondelete="CASCADE"), nullable=True
    )
    seller_lead_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("seller_leads.id", ondelete="CASCADE"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Ratio, nullable=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)

    investor: Mapped[Investor | None] = relationship(back_populates="evidence")
