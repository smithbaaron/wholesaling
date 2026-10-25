"""M1 Market Selection (REQUIREMENTS.md section 6; AGT-M1 "Haiku + code", the code half).

Scores every seed market from provider signals, writes the ``markets`` rows (tier, score,
signals document, deal band, ``last_scored_at``) and returns an ``M1Report`` ranked with
per-signal contributions and gate results. The report is what the human approves (M1-11):
M1 never puts a market in Active on its first sighting, and a Watch -> Active change needs
>=10 qualified buy boxes plus two consecutive crossings (M1-04, M1-07).

No model is called here. Deterministic code over ``MarketSignals`` is the whole agent; any
future model use (e.g. summarising the report) is additive and optional.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.scoring import (
    MarketScore,
    TierDecision,
    deal_band_for_market,
    market_score,
    tier_markets,
)
from wholesaling.config.tunables import Tunables, get_tunables
from wholesaling.db.base import utcnow
from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.enums import MarketTier
from wholesaling.db.models.investors import Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.repositories.markets import MarketRepository
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.models import MarketRef, MarketSignals
from wholesaling.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MarketReportRow:
    market: str
    market_id: uuid.UUID
    rank: int | None
    score: float | None
    tier_before: MarketTier | None
    tier_proposed: MarketTier
    tier_after: MarketTier
    crossings: int
    tier_reason: str
    data_gate_passed: bool
    legal_gate_passed: bool
    legal_review_pending: bool
    friction: int | None
    components: dict[str, float]
    contributions: dict[str, float]
    qualified_buy_boxes: int
    deal_band: tuple[Decimal, Decimal]
    median_price: Decimal | None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["market_id"] = str(self.market_id)
        d["tier_before"] = None if self.tier_before is None else self.tier_before.value
        d["tier_proposed"] = self.tier_proposed.value
        d["tier_after"] = self.tier_after.value
        d["deal_band"] = [str(self.deal_band[0]), str(self.deal_band[1])]
        d["median_price"] = None if self.median_price is None else str(self.median_price)
        return d


@dataclass(frozen=True, slots=True)
class M1Report:
    """Ranked markets with everything a human needs to approve Active markets (M1-11)."""

    scored_at: datetime
    active_cap: int
    promote_at_buy_boxes: int
    rows: list[MarketReportRow] = field(default_factory=list)  # ranked: scored first, then gated

    @property
    def active(self) -> list[MarketReportRow]:
        return [r for r in self.rows if r.tier_after is MarketTier.ACTIVE]

    @property
    def watch(self) -> list[MarketReportRow]:
        return [r for r in self.rows if r.tier_after is MarketTier.WATCH]

    @property
    def excluded(self) -> list[MarketReportRow]:
        return [r for r in self.rows if r.tier_after is MarketTier.EXCLUDED]

    @property
    def pending_changes(self) -> list[MarketReportRow]:
        """Markets whose proposed tier differs from the stored one but has crossed only once."""
        return [r for r in self.rows if r.tier_proposed is not r.tier_after]

    def to_json(self) -> dict[str, Any]:
        return {
            "scored_at": self.scored_at.isoformat(),
            "active_cap": self.active_cap,
            "promote_at_buy_boxes": self.promote_at_buy_boxes,
            "rows": [r.to_json() for r in self.rows],
        }


async def qualified_buy_box_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Current, active buy boxes per market via the investor's market (M1-04 ">=10 qualified
    buy boxes"). One investor with two distinct keys counts twice; versions count once."""
    stmt = (
        select(Investor.market_id, func.count())
        .select_from(BuyBox)
        .join(Investor, Investor.id == BuyBox.investor_id)
        .where(BuyBox.is_current.is_(True), BuyBox.is_active.is_(True))
        .where(Investor.market_id.is_not(None))
        .group_by(Investor.market_id)
    )
    rows = (await session.execute(stmt)).all()
    return {row[0]: int(row[1]) for row in rows if row[0] is not None}


class M1MarketSelection:
    def __init__(self, tunables: Tunables | None = None) -> None:
        self._tunables = tunables or get_tunables()

    async def run(
        self,
        session: AsyncSession,
        providers: DataProviders,
        config: Tunables | None = None,
        *,
        as_of: datetime | None = None,
    ) -> M1Report:
        tunables = config or self._tunables
        m1 = tunables.m1
        scored_at = as_of or utcnow()
        repo = MarketRepository(session)

        markets: dict[str, Market] = {}
        signals: dict[str, MarketSignals] = {}
        scores: dict[str, MarketScore] = {}
        for label in m1.seed_markets:
            ref = MarketRef.parse(label)
            market, created = await repo.upsert(ref.name, ref.state)
            if created:
                log.info("m1.market_created", market=ref.label, tier=market.tier.value)
            sig = await providers.signals.signals(ref)
            friction = m1.legal_friction.get(ref.state)
            markets[ref.label] = market
            signals[ref.label] = sig
            scores[ref.label] = market_score(sig, m1.weights, friction)

        bb_by_market_id = await qualified_buy_box_counts(session)
        buy_box_counts = {label: bb_by_market_id.get(m.id, 0) for label, m in markets.items()}
        # A row M1 has never scored (no last_scored_at) is untiered: it cannot jump to Active
        # on its first run even if a human pre-seeded the row.
        current_tiers: dict[str, MarketTier | None] = {
            label: (m.tier if m.last_scored_at is not None else None)
            for label, m in markets.items()
        }
        crossings = {label: repo.crossings(m) for label, m in markets.items()}

        decisions: dict[str, TierDecision] = tier_markets(
            scores,
            active_cap=m1.active_cap,
            promote_at_buy_boxes=m1.watch_to_active_min_buy_boxes,
            current_tiers=current_tiers,
            buy_box_counts=buy_box_counts,
            crossings=crossings,
        )

        rows: list[MarketReportRow] = []
        for label, market in markets.items():
            sc, sig, dec = scores[label], signals[label], decisions[label]
            band = deal_band_for_market(sig.median_price, tunables.deal_band)
            doc: dict[str, Any] = {
                "raw": sig.to_json(),
                "components": sc.components,
                "contributions": sc.contributions,
                "gates": {
                    "data": sc.data_gate_passed,
                    "legal": sc.legal_gate_passed,
                    "legal_review_pending": sc.legal_review_pending,
                    "friction": sc.friction,
                },
                "qualified_buy_boxes": buy_box_counts[label],
                "tier_proposed": dec.proposed.value,
                "tier_reason": dec.reason,
                "rank": dec.rank,
            }
            await repo.record_scoring(
                market, score=sc.score, signals=doc, deal_band=band, scored_at=scored_at
            )
            await repo.update_tier(market, dec.new, crossings=dec.crossings)
            rows.append(
                MarketReportRow(
                    market=label,
                    market_id=market.id,
                    rank=dec.rank,
                    score=sc.score,
                    tier_before=dec.current,
                    tier_proposed=dec.proposed,
                    tier_after=dec.new,
                    crossings=dec.crossings,
                    tier_reason=dec.reason,
                    data_gate_passed=sc.data_gate_passed,
                    legal_gate_passed=sc.legal_gate_passed,
                    legal_review_pending=sc.legal_review_pending,
                    friction=sc.friction,
                    components=dict(sc.components),
                    contributions=dict(sc.contributions),
                    qualified_buy_boxes=buy_box_counts[label],
                    deal_band=band,
                    median_price=sig.median_price,
                )
            )

        rows.sort(key=lambda r: (r.rank is None, r.rank or 0, -(r.score or 0.0), r.market))
        report = M1Report(
            scored_at=scored_at,
            active_cap=m1.active_cap,
            promote_at_buy_boxes=m1.watch_to_active_min_buy_boxes,
            rows=rows,
        )
        log.info(
            "m1.run_complete",
            markets=len(rows),
            active=len(report.active),
            watch=len(report.watch),
            excluded=len(report.excluded),
            pending_changes=len(report.pending_changes),
        )
        return report


__all__ = ["M1MarketSelection", "M1Report", "MarketReportRow", "qualified_buy_box_counts"]
