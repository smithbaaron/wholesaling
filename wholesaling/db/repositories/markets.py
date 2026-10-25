"""Markets: M1 state and the A1 discovery lock (REQUIREMENTS.md M1-04, M1-07, WF-09).

The ``markets.signals`` JSON document holds, besides the raw M1 inputs, two pieces of operating
state that have no dedicated column (the model is not altered here):

- ``tier_crossings`` (int): consecutive rescoring runs in which the proposed tier differed
  from the stored tier (M1-07 "threshold crossed twice").
- ``a1_running_since`` (ISO-8601 UTC): set while an A1 run holds the market; cleared on
  release; treated as stale after ``DEFAULT_LOCK_TTL`` so a crashed worker cannot block the
  daily schedule forever (WF-09 "no overlapping runs").
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.base import utcnow
from wholesaling.db.models.enums import MarketTier
from wholesaling.db.models.markets import Market

DEFAULT_LOCK_TTL = timedelta(hours=6)
LOCK_KEY = "a1_running_since"
CROSSINGS_KEY = "tier_crossings"

# IANA zone per state for the WF-09 "06:00 local" schedule. Metros that straddle zones
# (e.g. Kansas City, Nashville) use the zone of the principal city.
STATE_TIMEZONES: dict[str, str] = {
    "TX": "America/Chicago",
    "OK": "America/Chicago",
    "MO": "America/Chicago",
    "AL": "America/Chicago",
    "TN": "America/Chicago",
    "GA": "America/New_York",
    "FL": "America/New_York",
    "NC": "America/New_York",
    "IN": "America/Indiana/Indianapolis",
    "OH": "America/New_York",
    "MI": "America/Detroit",
    "AZ": "America/Phoenix",
}


class MarketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, market_id: uuid.UUID) -> Market | None:
        return await self._s.get(Market, market_id)

    async def get_by_name_state(self, name: str, state: str) -> Market | None:
        stmt = select(Market).where(Market.name == name, Market.state == state.upper())
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[Market]:
        stmt = select(Market).order_by(Market.state, Market.name)
        return list((await self._s.execute(stmt)).scalars().all())

    async def list_by_tier(self, tier: MarketTier) -> list[Market]:
        stmt = select(Market).where(Market.tier == tier).order_by(Market.score.desc(), Market.name)
        return list((await self._s.execute(stmt)).scalars().all())

    async def upsert(
        self,
        name: str,
        state: str,
        *,
        timezone: str | None = None,
    ) -> tuple[Market, bool]:
        """Create the market in ``Watch`` if absent (M1-04: nothing starts Active). Returns
        ``(market, created)``. Existing rows are left untouched here; scoring and tiering
        have their own methods."""
        state = state.upper()
        existing = await self.get_by_name_state(name, state)
        if existing is not None:
            return existing, False
        market = Market(
            name=name,
            state=state,
            tier=MarketTier.WATCH,
            timezone=timezone or STATE_TIMEZONES.get(state, "America/Chicago"),
            signals={},
        )
        self._s.add(market)
        await self._s.flush()
        return market, True

    async def record_scoring(
        self,
        market: Market,
        *,
        score: float | None,
        signals: dict[str, Any],
        deal_band: tuple[Decimal, Decimal] | None,
        scored_at: datetime | None = None,
    ) -> Market:
        """Store the M1 result. ``signals`` replaces the scoring part of the JSON document;
        operating keys (lock, crossings) are preserved."""
        doc = {k: v for k, v in market.signals.items() if k in (LOCK_KEY, CROSSINGS_KEY)}
        doc.update(signals)
        market.signals = doc
        market.score = None if score is None else Decimal(str(round(score, 4)))
        if deal_band is not None:
            market.deal_band_min, market.deal_band_max = deal_band
        market.last_scored_at = scored_at or utcnow()
        await self._s.flush()
        return market

    def crossings(self, market: Market) -> int:
        return int(market.signals.get(CROSSINGS_KEY, 0) or 0)

    async def update_tier(self, market: Market, tier: MarketTier, *, crossings: int) -> Market:
        """Apply a tier decision and persist the crossing counter (M1-07)."""
        market.tier = tier
        market.signals = {**market.signals, CROSSINGS_KEY: int(crossings)}
        await self._s.flush()
        return market

    # ---- A1 discovery lock (WF-09) -------------------------------------------------------

    def lock_held_since(self, market: Market) -> datetime | None:
        raw = market.signals.get(LOCK_KEY)
        if not raw:
            return None
        ts = datetime.fromisoformat(str(raw))
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

    async def acquire_discovery_lock(
        self,
        market_id: uuid.UUID,
        *,
        now: datetime | None = None,
        ttl: timedelta = DEFAULT_LOCK_TTL,
    ) -> bool:
        """Take the per-market A1 lock. Returns ``False`` when another run holds a fresh lock.
        A lock older than ``ttl`` is stale (crashed worker) and is taken over."""
        now = now or utcnow()
        stmt = select(Market).where(Market.id == market_id).with_for_update()
        market = (await self._s.execute(stmt)).scalar_one_or_none()
        if market is None:
            msg = f"unknown market {market_id}"
            raise LookupError(msg)
        held = self.lock_held_since(market)
        if held is not None and now - held < ttl:
            return False
        market.signals = {**market.signals, LOCK_KEY: now.isoformat()}
        await self._s.flush()
        return True

    async def release_discovery_lock(self, market_id: uuid.UUID) -> None:
        market = await self.get(market_id)
        if market is None or LOCK_KEY not in market.signals:
            return
        market.signals = {k: v for k, v in market.signals.items() if k != LOCK_KEY}
        await self._s.flush()


__all__ = [
    "CROSSINGS_KEY",
    "DEFAULT_LOCK_TTL",
    "LOCK_KEY",
    "STATE_TIMEZONES",
    "MarketRepository",
]
