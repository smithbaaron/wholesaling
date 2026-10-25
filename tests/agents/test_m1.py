"""M1 end to end on the mock signals provider (M1-01..07, M1-11)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.m1 import M1MarketSelection, qualified_buy_box_counts
from wholesaling.config.tunables import M1Config, Tunables
from wholesaling.db.models import BuyBox, Investor
from wholesaling.db.models.enums import MarketTier
from wholesaling.db.normalize import normalize_entity_name
from wholesaling.db.repositories.markets import CROSSINGS_KEY, MarketRepository
from wholesaling.integrations.data.interface import DataProviders


async def _add_buy_boxes(session: AsyncSession, market_id: uuid.UUID, n: int) -> None:
    for _ in range(n):
        name = f"Box Holder {uuid.uuid4().hex[:10]} LLC"
        inv = Investor(
            canonical_name=name,
            entity_key=normalize_entity_name(name),
            aliases=[name],
            market_id=market_id,
        )
        session.add(inv)
        await session.flush()
        session.add(BuyBox(investor_id=inv.id, version=1))
        # An older superseded version of the same key must not count twice.
        session.add(BuyBox(investor_id=inv.id, buy_box_key="other", version=1, is_current=False))
    await session.flush()


async def test_first_run_scores_all_seed_markets_and_promotes_none(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    report = await M1MarketSelection().run(session, providers, as_of=as_of)
    assert len(report.rows) == 21
    assert report.active == []  # M1-11: Active is a human approval, never a first-run outcome
    assert len(report.watch) == 20
    assert [r.market for r in report.excluded] == ["Birmingham, AL"]
    assert report.rows[0].market == "Houston, TX" and report.rows[0].rank == 1
    assert report.rows[-1].market == "Birmingham, AL" and report.rows[-1].rank is None
    for r in report.rows:
        assert r.tier_before is None
        assert r.tier_after is not MarketTier.ACTIVE
        assert r.crossings == 0
    markets = await MarketRepository(session).list_all()
    assert len(markets) == 21
    assert all(m.last_scored_at is not None for m in markets)


async def test_report_rows_carry_per_signal_contributions_and_gates(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    report = await M1MarketSelection().run(session, providers, as_of=as_of)
    houston = report.rows[0]
    assert set(houston.contributions) == {
        "demand",
        "spread",
        "distress",
        "liquidity",
        "price_fit",
        "friction",
    }
    assert houston.score is not None
    assert houston.score == round(sum(houston.contributions.values()), 6)
    assert houston.data_gate_passed and houston.legal_gate_passed
    assert houston.friction == 1 and houston.legal_review_pending is False
    atlanta = next(r for r in report.rows if r.market == "Atlanta, GA")
    assert atlanta.legal_review_pending is True  # no GA entry in config/m1.yaml yet
    birmingham = report.rows[-1]
    assert birmingham.data_gate_passed is False and birmingham.components == {}


async def test_market_rows_hold_tier_score_signals_band_and_timestamp(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    await M1MarketSelection().run(session, providers, as_of=as_of)
    repo = MarketRepository(session)
    houston = await repo.get_by_name_state("Houston", "TX")
    assert houston is not None
    assert houston.tier is MarketTier.WATCH
    assert houston.score is not None and Decimal("0") < houston.score < Decimal("1")
    assert houston.last_scored_at.replace(tzinfo=None) == as_of.replace(
        tzinfo=None
    )  # SQLite drops tz
    # M1-06: median 262,000 -> 131,000 .. 393,000 clamped to 350,000
    assert (houston.deal_band_min, houston.deal_band_max) == (Decimal("131000"), Decimal("350000"))
    assert houston.signals["raw"]["market"] == {"name": "Houston", "state": "TX"}
    assert houston.signals["gates"]["data"] is True
    assert houston.signals["tier_proposed"] == "Watch"
    assert houston.signals[CROSSINGS_KEY] == 0
    birmingham = await repo.get_by_name_state("Birmingham", "AL")
    assert birmingham is not None
    assert birmingham.tier is MarketTier.EXCLUDED and birmingham.score is None
    assert (birmingham.deal_band_min, birmingham.deal_band_max) == (
        Decimal("75000"),
        Decimal("350000"),
    )


async def test_rerun_is_stable_and_creates_no_duplicate_rows(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    first = await M1MarketSelection().run(session, providers, as_of=as_of)
    second = await M1MarketSelection().run(session, providers, as_of=as_of + timedelta(days=30))
    assert len(await MarketRepository(session).list_all()) == 21
    assert [r.market for r in first.rows] == [r.market for r in second.rows]
    assert [r.score for r in first.rows] == [r.score for r in second.rows]
    assert second.active == [] and second.pending_changes == []


async def test_watch_to_active_needs_buy_boxes_and_two_crossings(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    m1 = M1MarketSelection()
    await m1.run(session, providers, as_of=as_of)
    repo = MarketRepository(session)
    houston = await repo.get_by_name_state("Houston", "TX")
    assert houston is not None

    # Run 2: still no buy boxes -> Watch, no crossing.
    r2 = await m1.run(session, providers, as_of=as_of + timedelta(days=30))
    row = next(r for r in r2.rows if r.market == "Houston, TX")
    assert row.tier_proposed is MarketTier.WATCH and row.tier_reason == "below_buy_box_threshold"

    # Nine boxes: one short of M1-04's ten.
    await _add_buy_boxes(session, houston.id, 9)
    assert (await qualified_buy_box_counts(session))[houston.id] == 9
    r3 = await m1.run(session, providers, as_of=as_of + timedelta(days=60))
    row = next(r for r in r3.rows if r.market == "Houston, TX")
    assert row.qualified_buy_boxes == 9 and row.tier_after is MarketTier.WATCH

    # Ten boxes: first crossing -> proposed Active, still Watch (M1-07).
    await _add_buy_boxes(session, houston.id, 1)
    r4 = await m1.run(session, providers, as_of=as_of + timedelta(days=90))
    row = next(r for r in r4.rows if r.market == "Houston, TX")
    assert row.tier_proposed is MarketTier.ACTIVE
    assert row.tier_after is MarketTier.WATCH
    assert row.crossings == 1
    assert row in r4.pending_changes
    assert r4.active == []

    # Second consecutive crossing -> Active.
    r5 = await m1.run(session, providers, as_of=as_of + timedelta(days=120))
    row = next(r for r in r5.rows if r.market == "Houston, TX")
    assert row.tier_after is MarketTier.ACTIVE and row.crossings == 0
    assert [r.market for r in r5.active] == ["Houston, TX"]
    await session.refresh(houston)
    assert houston.tier is MarketTier.ACTIVE
    # Only TX has a friction entry, so even with buy boxes a GA market could not be promoted.
    atlanta = next(r for r in r5.rows if r.market == "Atlanta, GA")
    assert atlanta.legal_review_pending


async def test_pre_seeded_active_row_never_scored_is_treated_as_untiered(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    repo = MarketRepository(session)
    market, _ = await repo.upsert("Houston", "TX")
    market.tier = MarketTier.ACTIVE  # a human hand-edit before any M1 run
    await session.flush()
    await _add_buy_boxes(session, market.id, 12)
    report = await M1MarketSelection().run(session, providers, as_of=as_of)
    row = next(r for r in report.rows if r.market == "Houston, TX")
    assert row.tier_before is None
    assert row.tier_after is MarketTier.WATCH
    assert row.tier_reason == "new_market_starts_in_watch"


async def test_active_cap_from_config_is_respected(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    tunables = Tunables()
    base = tunables.m1
    small = Tunables(
        m1=M1Config(
            weights=base.weights,
            active_cap=1,
            watch_to_active_min_buy_boxes=1,
            legal_friction={"TX": 1, "MI": 0},
            seed_markets=["Houston, TX", "Detroit, MI"],
        )
    )
    m1 = M1MarketSelection(small)
    await m1.run(session, providers, as_of=as_of)
    repo = MarketRepository(session)
    for name, state in (("Houston", "TX"), ("Detroit", "MI")):
        m = await repo.get_by_name_state(name, state)
        assert m is not None
        await _add_buy_boxes(session, m.id, 1)
    for i in range(2):
        report = await m1.run(session, providers, as_of=as_of + timedelta(days=30 * (i + 1)))
    assert [r.market for r in report.active] == ["Houston, TX"]
    detroit = next(r for r in report.rows if r.market == "Detroit, MI")
    assert detroit.tier_after is MarketTier.WATCH and detroit.tier_reason == "outside_active_cap"


async def test_report_serialises_to_json(
    session: AsyncSession, providers: DataProviders, as_of: datetime
) -> None:
    import json

    report = await M1MarketSelection().run(session, providers, as_of=as_of)
    payload = json.loads(json.dumps(report.to_json()))
    assert payload["active_cap"] == 5 and payload["promote_at_buy_boxes"] == 10
    assert payload["rows"][0]["market"] == "Houston, TX"
    assert payload["rows"][0]["deal_band"] == ["131000", "350000"]
