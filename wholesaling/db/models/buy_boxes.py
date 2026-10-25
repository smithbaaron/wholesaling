"""``buy_boxes`` (versioned) and ``search_profiles`` (REQUIREMENTS.md BB-01..06, DM-03, DM-05)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from wholesaling.db.models.enums import SearchProfileStatus, enum_column
from wholesaling.db.types import GUID, JSONDoc, Money

DEFAULT_BUY_BOX_KEY = "default"


class BuyBox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One version of one of an investor's buy boxes.

    An investor can hold several distinct buy boxes (BB-03); ``buy_box_key`` identifies the
    criteria set (A4 assigns it, ``"default"`` when the investor has only one). Every change
    is a new ``version`` row for the same key; exactly one row per key has ``is_current``.
    Uniqueness on ``(investor_id, buy_box_key, version)`` is the backstop against duplicates
    (ADR 0002). Structured fields (DM-03) sit beside the investor's original words.
    """

    __tablename__ = "buy_boxes"
    __table_args__ = (
        UniqueConstraint("investor_id", "buy_box_key", "version"),
        Index("ix_buy_boxes_investor_id", "investor_id"),
        Index("ix_buy_boxes_investor_current", "investor_id", "is_current"),
        Index("ix_buy_boxes_source_message_id", "source_message_id"),
    )

    investor_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("investors.id", ondelete="CASCADE"), nullable=False
    )
    buy_box_key: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_BUY_BOX_KEY
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Geography: {"markets": [...], "cities": [...], "zips": [...], "neighborhoods": [...]}
    geography: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    asset_types: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    price_min: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    price_max: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    condition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    financing: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closing_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONDoc(), nullable=False, default=dict
    )
    # Anything explicitly stated that has no column (BB-01 "any other explicitly stated criteria").
    other_criteria: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    # Fields A4 asked about but the investor has not answered (INV-14 "track unknowns").
    unknown_fields: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The inbound message this version was extracted from. Replaying it is a no-op.
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    change_note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SearchProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """D1 output: normalised, versioned search criteria for one buy box (BB-04, DM-05)."""

    __tablename__ = "search_profiles"
    __table_args__ = (
        UniqueConstraint("buy_box_id", "version"),
        Index("ix_search_profiles_buy_box_id", "buy_box_id"),
        Index("ix_search_profiles_status", "status"),
    )

    buy_box_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("buy_boxes.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    unknowns: Mapped[list[str]] = mapped_column(JSONDoc(), nullable=False, default=list)
    status: Mapped[SearchProfileStatus] = mapped_column(
        enum_column(SearchProfileStatus, "search_profile_status"),
        nullable=False,
        default=SearchProfileStatus.ACTIVE,
    )
    # Hash of ``criteria`` so BB-05 can skip searches for an unchanged revision.
    criteria_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
