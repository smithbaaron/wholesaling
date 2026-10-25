"""``markets`` — M1 market selection state (REQUIREMENTS.md section 6, ENG-Q5)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from wholesaling.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from wholesaling.db.models.enums import MarketTier, enum_column
from wholesaling.db.types import JSONDoc, Money


class Market(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "markets"
    __table_args__ = (
        UniqueConstraint("name", "state"),
        Index("ix_markets_tier", "tier"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    tier: Mapped[MarketTier] = mapped_column(
        enum_column(MarketTier, "market_tier"), nullable=False, default=MarketTier.WATCH
    )
    # IANA zone for local schedules (WF-09 "06:00 local per market").
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Chicago")
    # Deal band on purchase price (M1-06). Null means the global default from config applies.
    deal_band_min: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    deal_band_max: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    signals: Mapped[dict[str, Any]] = mapped_column(JSONDoc(), nullable=False, default=dict)
    last_scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
