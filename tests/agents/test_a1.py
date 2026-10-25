"""A1 end to end on the Houston fixture (INV-01..07), idempotency, and the WF-09 lock."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wholesaling.activities import discovery
from wholesaling.activities.discovery import DiscoveryLocked, discovery_lock, run_a1
from wholesaling.agents.a1 import A1InvestorDiscovery
from wholesaling.db.models import Investor, Market, SourceEvidence
from wholesaling.db.models.enums import InvestorType, MarketTier
from wholesaling.db.repositories.investors import InvestorRepository
from wholesaling.db.repositories.markets import LOCK_KEY, MarketRepository
from wholesaling.integrations.data.interface import DataProviders

# Hand-derived from the Houston fixture scenarios (docs/agents/discovery.md). Global deal band.
EXPECTED_FOUND = 22  # 17 named scenarios + 5 retail buyers on the resale deeds
EXPECTED_QUALIFIED = 7
EXPECTED_PROSPECTS = 7
EXPECTED_PRIORITY = 3
EXPECTED_EXCLUDED = {
    "builder_lots": 1,  # Lennar Homes of Texas LLC
    "government": 1,  # Harris County
    "individual_below_two_purchases": 6,  # John Carter + 5 retail resale buyers
    "institutional": 1,  # Opendoor Property Trust I
    "lender_reo": 1,  # Wells Fargo Bank NA
    "no_cash_purchase": 1,  # Westside Living LLC (conventional mortgages)
    "nonprofit": 1,  # Habitat for Humanity Houston Inc
    "outside_deal_band": 1,  # Luxury Estates Group LLC
    "stale_last_purchase": 1,  # Old Money Investments LLC
    "suppressed": 1,  # Suppressed Holdings LLC
}
EXPECTED_SCORES = {
    "Bayou Capital Partners LLC": (89, InvestorType.FLIPPER),
    "Gulf Coast Rentals LLC": (89, InvestorType.BUY_AND_HOLD),
    "Lone Star Flips LLC": (76, InvestorType.FLIPPER),
    "Fast Close Houston LLC": (72, InvestorType.WHOLESALER),
    "Keller Realty Investments LLC": (72, InvestorType.AGENT_INVESTOR),
    "Heights Holdings LLC": (72, InvestorType.UNKNOWN),
    "Maria Hernandez": (67, InvestorType.UNKNOWN),
}


@pytest.fixture
async def houston(session: AsyncSession) -> Market:
    market, _ = await MarketRepository(session).upsert("Houston", "TX")
    return market


async def _count(session: AsyncSession, model: type) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_houston_mock_run_counts_are_exact(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    res = await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    assert res.deeds_pulled == 47
    assert res.found == EXPECTED_FOUND
    assert res.qualified == EXPECTED_QUALIFIED
    assert res.prospects == EXPECTED_PROSPECTS
    assert res.priority == EXPECTED_PRIORITY
    assert res.excluded_by_reason == EXPECTED_EXCLUDED
    assert res.by_type == {
        "agent_investor": 1,
        "buy_and_hold": 1,
        "flipper": 2,
        "unknown": 2,
        "wholesaler": 1,
    }
    assert res.found == res.qualified + sum(res.excluded_by_reason.values())
    assert res.investors_created == 13  # 7 qualified + 6 INV-05 exclusions
    assert res.investors_updated == 0
    assert len(res.prospect_ids) == 7


async def test_scores_and_types_match_hand_calculation(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    repo = InvestorRepository(session)
    for name, (score, itype) in EXPECTED_SCORES.items():
        inv = await repo.get_by_name(name)
        assert inv is not None, name
        assert inv.score == score, name
        assert inv.investor_type is itype, name
        assert inv.market_id == houston.id
        assert inv.excluded_reason is None
        assert set(inv.score_breakdown) >= {
            "volume",
            "recency",
            "cash_rate",
            "band_fit",
            "concentration",
            "type_bonus",
            "features",
        }


async def test_llc_variants_and_shared_address_collapse_with_all_evidence(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    repo = InvestorRepository(session)
    bayou = await repo.get_by_name("Bayou Capital Partners LLC")
    assert bayou is not None
    assert set(bayou.aliases) == {
        "Bayou Capital Partners LLC",
        "BAYOU CAPITAL PARTNERS, L.L.C.",
        "Bayou Capital Partners Limited Liability Company",
    }
    evidence = await repo.list_evidence(bayou.id)
    assert len(evidence) == 6  # 4 purchases + 2 resales
    assert {e.raw["role"] for e in evidence} == {"purchase", "resale"}
    assert bayou.purchase_count_24m == 4
    assert bayou.mailing_address_key is not None and "POST OAK" in bayou.mailing_address_key
    assert (
        bayou.source_refs["mock_county_deeds"] and len(bayou.source_refs["mock_county_deeds"]) == 6
    )

    gulf = await repo.get_by_name("Gulf Coast Rentals LLC")
    assert gulf is not None
    assert "Gulf Coast Rentals II LLC" in gulf.aliases
    assert await repo.get_by_name("Gulf Coast Rentals II LLC") is None  # merged, not a second row
    assert len(await repo.list_evidence(gulf.id)) == 5

    # Same registered agent, different names: two rows (INV-06 conservative merge).
    assert await repo.get_by_name("Fast Close Houston LLC") is not None
    assert await repo.get_by_name("Heights Holdings LLC") is not None


async def test_exclusions_are_written_with_reason_and_not_scored(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    repo = InvestorRepository(session)
    opendoor = await repo.get_by_name("Opendoor Property Trust I")
    assert opendoor is not None
    assert opendoor.excluded_reason == "institutional"
    assert opendoor.investor_type is InvestorType.IBUYER_INSTITUTIONAL
    assert opendoor.score is None
    assert len(await repo.list_evidence(opendoor.id)) == 3
    for name, reason in (
        ("Lennar Homes of Texas LLC", "builder_lots"),
        ("Harris County", "government"),
        ("Habitat for Humanity Houston Inc", "nonprofit"),
        ("Wells Fargo Bank NA", "lender_reo"),
        ("Suppressed Holdings LLC", "suppressed"),
    ):
        inv = await repo.get_by_name(name)
        assert inv is not None and inv.excluded_reason == reason and inv.score is None


async def test_non_qualified_candidates_are_counted_not_written(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    repo = InvestorRepository(session)
    for name in (
        "John Carter",
        "Westside Living LLC",
        "Luxury Estates Group LLC",
        "Old Money Investments LLC",
        "Daniel Rao",
    ):
        assert await repo.get_by_name(name) is None, name
    assert await _count(session, Investor) == 13


async def test_individual_repeat_buyer_gets_first_name_and_is_not_entity(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    maria = await InvestorRepository(session).get_by_name("Maria Hernandez")
    assert maria is not None
    assert maria.is_entity is False and maria.first_name == "Maria"
    bayou = await InvestorRepository(session).get_by_name("Bayou Capital Partners LLC")
    assert bayou is not None and bayou.is_entity is True and bayou.first_name is None


async def test_rerun_same_window_is_idempotent(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    a1 = A1InvestorDiscovery()
    first = await a1.run(session, providers, houston, as_of=as_of)
    investors, evidence = await _count(session, Investor), await _count(session, SourceEvidence)
    second = await a1.run(session, providers, houston, as_of=as_of)
    assert await _count(session, Investor) == investors == 13
    assert await _count(session, SourceEvidence) == evidence == 38
    assert second.investors_created == 0 and second.investors_updated == 13
    assert second.evidence_added == 0
    volatile = {"prospect_ids", "investors_created", "investors_updated", "evidence_added"}
    assert {k: v for k, v in second.to_json().items() if k not in volatile} == {
        k: v for k, v in first.to_json().items() if k not in volatile
    }
    assert set(second.prospect_ids) == set(first.prospect_ids)
    bayou = await InvestorRepository(session).get_by_name("Bayou Capital Partners LLC")
    assert bayou is not None and bayou.score == 89 and len(bayou.aliases) == 3


async def test_prospect_ids_are_ordered_by_score_desc(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    res = await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    scores = []
    for pid in res.prospect_ids:
        inv = await session.get(Investor, pid)
        assert inv is not None and inv.score is not None
        scores.append(inv.score)
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 89 and scores[-1] == 67


async def test_market_deal_band_override_changes_band_fit(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    # M1-06 band for Houston after an M1 run: 131k..350k. Gulf Coast's 95k..128k buys fall out.
    houston.deal_band_min, houston.deal_band_max = Decimal("131000"), Decimal("350000")
    await session.flush()
    res = await A1InvestorDiscovery().run(session, providers, houston, as_of=as_of)
    gulf = await InvestorRepository(session).get_by_name("Gulf Coast Rentals LLC")
    assert gulf is not None
    assert gulf.score == 77  # band fit 1/5 -> 3 instead of 15 (89 - 12)
    assert gulf.score_breakdown["band_fit"] == 3
    assert res.qualified == EXPECTED_QUALIFIED  # one in-band record is still enough


async def test_window_excludes_purchases_older_than_24_months(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    # Move the clock 5 months on: Old Money's 20-23 month purchases are now 25-28 months old and
    # leave the window entirely, so the candidate is not even "found".
    later = as_of + timedelta(days=5 * 30)
    res = await A1InvestorDiscovery().run(session, providers, houston, as_of=later)
    assert "stale_last_purchase" not in res.excluded_by_reason
    assert res.found == EXPECTED_FOUND - 1


async def test_runtime_suppression_keys_are_applied(
    session: AsyncSession, providers: DataProviders, houston: Market, as_of: datetime
) -> None:
    res = await A1InvestorDiscovery().run(
        session,
        providers,
        houston,
        as_of=as_of,
        suppressed_entity_keys=frozenset({"BAYOU CAPITAL PARTNERS LLC"}),
    )
    assert res.excluded_by_reason["suppressed"] == 2
    assert res.qualified == EXPECTED_QUALIFIED - 1
    bayou = await InvestorRepository(session).get_by_name("Bayou Capital Partners LLC")
    assert bayou is not None and bayou.excluded_reason == "suppressed"


# ---------------------------------------------------------------------------- WF-09 lock


async def test_discovery_lock_blocks_second_run_and_releases(
    session: AsyncSession, houston: Market, as_of: datetime
) -> None:
    repo = MarketRepository(session)
    assert await repo.acquire_discovery_lock(houston.id, now=as_of) is True
    assert await repo.acquire_discovery_lock(houston.id, now=as_of + timedelta(minutes=5)) is False
    assert repo.lock_held_since(houston) == as_of
    await repo.release_discovery_lock(houston.id)
    assert LOCK_KEY not in houston.signals
    assert await repo.acquire_discovery_lock(houston.id, now=as_of) is True


async def test_stale_lock_is_taken_over(
    session: AsyncSession, houston: Market, as_of: datetime
) -> None:
    repo = MarketRepository(session)
    assert await repo.acquire_discovery_lock(houston.id, now=as_of)
    assert await repo.acquire_discovery_lock(houston.id, now=as_of + timedelta(hours=5)) is False
    assert await repo.acquire_discovery_lock(houston.id, now=as_of + timedelta(hours=7)) is True
    assert repo.lock_held_since(houston) == as_of + timedelta(hours=7)


async def test_lock_does_not_disturb_other_signal_keys(
    session: AsyncSession, houston: Market, as_of: datetime
) -> None:
    repo = MarketRepository(session)
    await repo.record_scoring(houston, score=0.5, signals={"raw": {"x": 1}}, deal_band=None)
    await repo.update_tier(houston, MarketTier.WATCH, crossings=1)
    assert await repo.acquire_discovery_lock(houston.id, now=as_of)
    assert houston.signals["raw"] == {"x": 1} and houston.signals["tier_crossings"] == 1
    await repo.record_scoring(houston, score=0.6, signals={"raw": {"x": 2}}, deal_band=None)
    assert houston.signals[LOCK_KEY] == as_of.isoformat()  # scoring preserves the lock
    await repo.release_discovery_lock(houston.id)
    assert houston.signals["tier_crossings"] == 1


async def test_lock_unknown_market_raises(session: AsyncSession) -> None:
    import uuid

    with pytest.raises(LookupError):
        await MarketRepository(session).acquire_discovery_lock(uuid.uuid4())


async def test_discovery_lock_context_manager_and_activity(
    session_factory: async_sessionmaker[AsyncSession],
    providers: DataProviders,
    as_of: datetime,
) -> None:
    async with session_factory() as s, s.begin():
        market, _ = await MarketRepository(s).upsert("Houston", "TX")
        market_id = market.id
    discovery.configure(session_factory, providers)
    try:
        async with discovery_lock(session_factory, market_id, now=as_of):
            with pytest.raises(DiscoveryLocked):
                async with discovery_lock(session_factory, market_id, now=as_of):
                    pass
            # The activity sees the lock and skips instead of failing (WF-09).
            skipped = await run_a1(str(market_id), as_of=as_of.isoformat())
            assert skipped == {"skipped": "locked", "market_id": str(market_id)}
        # Released: a real run goes through and releases again.
        result = await run_a1(str(market_id), as_of=as_of.isoformat())
        assert result["found"] == EXPECTED_FOUND and result["priority"] == EXPECTED_PRIORITY
        async with session_factory() as s:
            m = await MarketRepository(s).get(market_id)
            assert m is not None and LOCK_KEY not in m.signals
            assert await _count(s, Investor) == 13
    finally:
        discovery.reset()


async def test_run_a1_releases_lock_when_the_agent_fails(
    session_factory: async_sessionmaker[AsyncSession], providers: DataProviders, as_of: datetime
) -> None:
    async with session_factory() as s, s.begin():
        market, _ = await MarketRepository(s).upsert("Houston", "TX")
        market_id = market.id

    class Boom(Exception):
        pass

    class FailingDeeds:
        name = "deed_records"
        mode = "mock"

        async def purchases(self, *_a: object, **_k: object) -> list:  # type: ignore[type-arg]
            raise Boom

        async def healthcheck(self) -> object:  # pragma: no cover
            raise NotImplementedError

    from dataclasses import replace

    discovery.configure(session_factory, replace(providers, deeds=FailingDeeds()))
    try:
        with pytest.raises(Boom):
            await run_a1(str(market_id), as_of=as_of.isoformat())
        async with session_factory() as s:
            m = await MarketRepository(s).get(market_id)
            assert m is not None and LOCK_KEY not in m.signals
    finally:
        discovery.reset()


async def test_run_m1_activity_returns_report_json(
    session_factory: async_sessionmaker[AsyncSession], providers: DataProviders, as_of: datetime
) -> None:
    discovery.configure(session_factory, providers)
    try:
        payload = await discovery.run_m1(as_of.isoformat())
        assert len(payload["rows"]) == 21
        assert all(r["tier_after"] != "Active" for r in payload["rows"])
    finally:
        discovery.reset()
