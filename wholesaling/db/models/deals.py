"""``property_candidates`` and ``matches`` — D2 output (REQUIREMENTS.md BB-07..09, DM-09); plus
``contracts`` — the BO-12 deal/transaction record (SEL-10, SEL-11, BB-11)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
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


# --------------------------------------------------------------------------------------------
# ``contracts`` - BO-12 (REQUIREMENTS.md SEL-10, SEL-11, BB-11, POL-03i/j, XC-05)
# --------------------------------------------------------------------------------------------


class ContractType(StrEnum):
    PSA = "psa"  # assignable purchase and sale agreement with the seller
    ASSIGNMENT = "assignment"  # assignment of that contract to the end buyer


class ContractStatus(StrEnum):
    REQUESTED = "requested"  # offer accepted; ``contract.requested`` written by A7/S5
    SENT_FOR_SIGNATURE = "sent_for_signature"  # a human generated the DocuSign envelope
    SIGNED = "signed"  # a human recorded the signature (never automated)
    CLOSED = "closed"  # a human recorded the closing (BB-11 realised economics)
    CANCELLED = "cancelled"  # deal dead / envelope voided


class EarnestMoneyStatus(StrEnum):
    """Human-only transitions (POL-03j): none -> requested -> deposited -> released."""

    NONE = "none"
    REQUESTED = "requested"
    DEPOSITED = "deposited"
    RELEASED = "released"


class BuyerResponse(StrEnum):
    INTERESTED = "interested"
    PASS = "pass"  # noqa: S105 - a buyer declining, not a credential


class Contract(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One signed-or-to-be-signed agreement on a deal.

    The PSA row is the deal record: deadlines, earnest money, the chosen buyer, deposit, title
    company, closing and deal-death live on it. The Assignment Agreement is a second row
    (``type = assignment``) pointing back through ``parent_contract_id``. Every status change is
    made by ``ContractRepository`` with an audit event; ``signed``, every earnest-money
    transition, ``closed`` and ``cancelled`` require ``actor = human`` (POL-06 hard floors).
    """

    __tablename__ = "contracts"
    __table_args__ = (
        UniqueConstraint("offer_id", "type"),
        Index("ix_contracts_seller_lead_id", "seller_lead_id"),
        Index("ix_contracts_status", "status"),
        Index("ix_contracts_buyer_investor_id", "buyer_investor_id"),
        Index("ix_contracts_parent_contract_id", "parent_contract_id"),
    )

    offer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("offers.id", ondelete="RESTRICT"), nullable=False
    )
    seller_lead_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("seller_leads.id", ondelete="RESTRICT"), nullable=False
    )
    parent_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contracts.id", ondelete="SET NULL"), nullable=True
    )
    type: Mapped[ContractType] = mapped_column(
        enum_column(ContractType, "contract_type", length=16), nullable=False
    )
    status: Mapped[ContractStatus] = mapped_column(
        enum_column(ContractStatus, "contract_status"),
        nullable=False,
        default=ContractStatus.REQUESTED,
    )
    property_address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    property_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    property_state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    property_postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    purchase_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    docusign_envelope_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    envelope_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sent_for_signature_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)  # human actor
    earnest_money_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    earnest_money_status: Mapped[EarnestMoneyStatus] = mapped_column(
        enum_column(EarnestMoneyStatus, "earnest_money_status", length=16),
        nullable=False,
        default=EarnestMoneyStatus.NONE,
    )
    option_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    inspection_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    buyer_investor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("investors.id", ondelete="SET NULL"), nullable=True
    )
    assignment_fee: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    realized_assignment_fee: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    deposit_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    deposit_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    title_company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deal_dead_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # S6 blast bookkeeping: {investor_id: {"response", "proof_of_funds", "deposit_terms", "at"}}
    buyer_responses: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    # Title/escrow coordination checklist: {"title_opened": bool, "earnest_delivered": bool, ...}
    checklist: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
