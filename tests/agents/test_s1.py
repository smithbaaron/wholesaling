"""S1 Seller List Builder: scoring, gates, dedupe, idempotent refresh (REQUIREMENTS.md SEL-01,
SEL-02, WF-10)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents import s1
from wholesaling.agents.s1 import (
    S1SellerListBuilder,
    gate_reason,
    is_long_term_hold,
    merge_records,
    owner_first_name,
    score_parcel,
    signal_score,
    stacked_signals,
)
from wholesaling.db.models.enums import MarketTier, SellerLeadStatus
from wholesaling.db.models.markets import Market
from wholesaling.db.models.sellers import SellerLead
from wholesaling.integrations.data.distress import DistressRecord, MockDistressListProvider
from wholesaling.integrations.data.interface import DataProviders
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.data.models import MarketRef

AS_OF = datetime(2026, 10, 25, tzinfo=UTC)
BAND = (Decimal("75000"), Decimal("350000"))


def rec(
    parcel: str = "P-1",
    *,
    signals: tuple[str, ...] = ("absentee", "tax_delinquent"),
    property_type: str = "sfr",
    units: int = 1,
    assessed: int | None = 150_000,
    estimated: int | None = None,
    last_sale: date | None = None,
    owner: str = "Jane Owner",
    address: str = "1 Test St",
) -> DistressRecord:
    return DistressRecord(
        parcel_id=parcel,
        address=address,
        city="Houston",
        state="TX",
        zip="77009",
        owner_name=owner,
        property_type=property_type,  # type: ignore[arg-type]
        units=units,
        signals=signals,
        source="test",
        source_ref=f"ref-{parcel}-{'-'.join(signals)}",
        retrieved_at=AS_OF,
        assessed_value=None if assessed is None else Decimal(assessed),
        estimated_value=None if estimated is None else Decimal(estimated),
        last_sale_date=last_sale,
    )


# -- pure rules ----------------------------------------------------------------------------------


def test_merge_records_groups_by_parcel_in_first_seen_order() -> None:
    groups = merge_records([rec("A"), rec("B"), rec("A", signals=("vacant",))])
    assert [g[0].parcel_id for g in groups] == ["A", "B"]
    assert len(groups[0]) == 2


def test_stacked_signals_unions_lists_and_derives_long_term_hold() -> None:
    rows = [rec("A", signals=("absentee",)), rec("A", signals=("pre_foreclosure", "absentee"))]
    assert stacked_signals(rows, AS_OF.date()) == ("absentee", "pre_foreclosure")
    held = [rec("B", signals=("absentee",), last_sale=date(2005, 1, 1))]
    assert stacked_signals(held, AS_OF.date()) == ("absentee", "long_term_hold")


@pytest.mark.parametrize(
    ("last_sale", "expected"),
    [
        (None, False),
        (date(2011, 10, 26), False),
        (date(2011, 10, 24), True),
        (date(1990, 1, 1), True),
    ],
)
def test_long_term_hold_is_fifteen_years(last_sale: date | None, expected: bool) -> None:
    assert is_long_term_hold(last_sale, AS_OF.date()) is expected


def test_signal_score_stacks_documented_weights() -> None:
    assert signal_score(("absentee", "tax_delinquent")) == 40
    assert signal_score(("probate", "vacant", "code_violation")) == 55
    assert signal_score(("pre_foreclosure",)) == 30
    assert signal_score(()) == 0
    assert set(s1.SIGNAL_WEIGHTS) == set(s1.DISTRESS_SIGNALS)


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (rec(), None),
        (rec(signals=("absentee",)), s1.REASON_TOO_FEW_SIGNALS),
        (rec(property_type="multifamily", units=12), s1.REASON_NOT_RESIDENTIAL),
        (rec(property_type="land", units=0), s1.REASON_NOT_RESIDENTIAL),
        (rec(units=5), s1.REASON_NOT_RESIDENTIAL),
        (rec(assessed=None), s1.REASON_NO_VALUE),
        (rec(assessed=520_000), s1.REASON_OUTSIDE_BAND),
        (rec(assessed=74_999), s1.REASON_OUTSIDE_BAND),
        (rec(assessed=None, estimated=200_000), None),
        (rec(property_type="fourplex", units=4), None),
    ],
)
def test_gate_reason(record: DistressRecord, reason: str | None) -> None:
    assert gate_reason(score_parcel([record], AS_OF.date()), BAND) == reason


def test_assessed_value_is_preferred_over_estimated() -> None:
    parcel = score_parcel(
        [rec(assessed=None, estimated=200_000), rec(assessed=180_000)], AS_OF.date()
    )
    assert parcel.value == Decimal(180_000)


@pytest.mark.parametrize(
    ("owner", "expected"),
    [
        ("Dolores Ramirez", "Dolores"),
        ("Estate of Walter Boyd", None),
        ("Linda & Mark Okafor", None),
        ("Ramirez, Dolores", None),
        ("Bayshore Apartments LP", None),
        ("Suppressed Holdings LLC", None),
        ("Cher", None),
        ("", None),
    ],
)
def test_owner_first_name(owner: str, expected: str | None) -> None:
    assert owner_first_name(owner) == expected


# -- the agent against SQLite ------------------------------------------------------------------


@pytest.fixture
def providers() -> DataProviders:
    return mock_data_providers()


async def _market(session: AsyncSession, tier: MarketTier = MarketTier.ACTIVE) -> Market:
    market = Market(name="Houston", state="TX", tier=tier)
    session.add(market)
    await session.flush()
    return market


async def _leads(session: AsyncSession) -> list[SellerLead]:
    return list(
        (await session.execute(select(SellerLead).order_by(SellerLead.parcel_id))).scalars()
    )


async def test_refuses_watch_and_excluded_markets(
    session: AsyncSession, providers: DataProviders
) -> None:
    for tier in (MarketTier.WATCH, MarketTier.EXCLUDED):
        market = Market(name=f"M-{tier.value}", state="TX", tier=tier)
        session.add(market)
        await session.flush()
        result = await S1SellerListBuilder().run(session, providers, market, as_of=AS_OF)
        assert result.refused and result.refused_reason == s1.REFUSED_NOT_ACTIVE
        assert result.records_pulled == 0 and result.queued == 0
    assert await _leads(session) == []
    assert isinstance(providers.distress, MockDistressListProvider)
    assert providers.distress.calls == []  # never even pulled the list


async def test_refuses_without_a_distress_provider(session: AsyncSession) -> None:
    bundle = mock_data_providers()
    providers = DataProviders(
        deeds=bundle.deeds,
        signals=bundle.signals,
        contacts=bundle.contacts,
        reference_lists=bundle.reference_lists,
    )
    market = await _market(session)
    result = await S1SellerListBuilder().run(session, providers, market, as_of=AS_OF)
    assert result.refused_reason == s1.REFUSED_NO_PROVIDER


async def test_houston_fixture_scenarios(session: AsyncSession, providers: DataProviders) -> None:
    market = await _market(session)
    result = await S1SellerListBuilder().run(session, providers, market, as_of=AS_OF)
    assert not result.refused
    assert result.records_pulled == 11 and result.parcels == 10  # Columbus row filtered out
    assert result.created == 5 and result.refreshed == 0 and result.queued == 5
    assert result.rejected_by_reason == {
        s1.REASON_NOT_RESIDENTIAL: 2,  # 12-unit building, land
        s1.REASON_NO_VALUE: 1,  # no assessed or estimated value to band
        s1.REASON_OUTSIDE_BAND: 1,  # $520k
        s1.REASON_SUPPRESSED: 1,  # Suppressed Holdings LLC on the reference list
    }
    leads = {lead.parcel_id: lead for lead in await _leads(session)}
    # Two lists for one parcel merged, plus the derived long-term hold (sold 2011).
    ramirez = leads["HCAD-D-0000001"]
    assert ramirez.signals == ["absentee", "tax_delinquent", "pre_foreclosure", "long_term_hold"]
    assert ramirez.signal_score == 80 and ramirez.status is SellerLeadStatus.QUEUED
    assert ramirez.parcel_verified and ramirez.address_key == "1420 PECAN GROVE DR|HOUSTON|TX|77009"
    assert len(ramirez.source_refs["distress"]) == 2
    # Probate + vacant + code violation (+ held since 1996).
    assert leads["HCAD-D-0000002"].signals == [
        "probate",
        "code_violation",
        "long_term_hold",
        "vacant",
    ]
    # One vendor signal plus the derived long-term hold (sold 1998).
    assert leads["HCAD-D-0000003"].signals == ["absentee", "long_term_hold"]
    # Duplex with two signals is residential 1-4.
    assert leads["HCAD-D-0000006"].signals == ["vacant", "expired_listing"]
    assert "HCAD-D-0000009" in leads
    for rejected in (
        "HCAD-D-0000004",
        "HCAD-D-0000005",
        "HCAD-D-0000007",
        "HCAD-D-0000008",
        "HCAD-D-0000010",
    ):
        assert rejected not in leads
    assert result.queued_lead_ids[0] == ramirez.id  # highest score first
    assert result.by_signal["absentee"] == 3  # Ramirez, Nguyen, Okafor


async def test_refresh_is_idempotent_and_updates_facts(
    session: AsyncSession, providers: DataProviders
) -> None:
    market = await _market(session)
    builder = S1SellerListBuilder()
    first = await builder.run(session, providers, market, as_of=AS_OF)
    await session.commit()
    second = await builder.run(session, providers, market, as_of=AS_OF)
    assert second.created == 0 and second.refreshed == first.created == 5
    count = (await session.execute(select(func.count()).select_from(SellerLead))).scalar_one()
    assert count == 5
    # A vendor adds a signal: the existing lead is refreshed, not duplicated.
    assert isinstance(providers.distress, MockDistressListProvider)
    providers.distress.records.append(
        rec(
            "HCAD-D-0000006",
            signals=("tax_delinquent",),
            owner="Gerald Pike",
            address="4410 Mesquite Trl",
        )
    )
    third = await builder.run(session, providers, market, as_of=AS_OF)
    assert third.created == 0
    pike = next(lead for lead in await _leads(session) if lead.parcel_id == "HCAD-D-0000006")
    assert pike.signals == ["tax_delinquent", "vacant", "expired_listing"]


async def test_dedupes_on_normalised_address_when_parcel_ids_differ(
    session: AsyncSession,
) -> None:
    market = await _market(session)
    distress = MockDistressListProvider(
        records=[
            rec("P-A", address="1420 Pecan Grove Drive"),
            rec("P-B", address="1420 PECAN GROVE DR."),
        ]
    )
    bundle = mock_data_providers()
    providers = DataProviders(
        deeds=bundle.deeds,
        signals=bundle.signals,
        contacts=bundle.contacts,
        reference_lists=bundle.reference_lists,
        distress=distress,
    )
    result = await S1SellerListBuilder().run(session, providers, market, as_of=AS_OF)
    assert result.created == 1 and result.refreshed == 1
    assert len(await _leads(session)) == 1


async def test_suppressed_or_dead_lead_is_never_requeued(
    session: AsyncSession, providers: DataProviders
) -> None:
    market = await _market(session)
    builder = S1SellerListBuilder()
    await builder.run(session, providers, market, as_of=AS_OF)
    lead = next(x for x in await _leads(session) if x.parcel_id == "HCAD-D-0000001")
    lead.status = SellerLeadStatus.SUPPRESSED
    await session.flush()
    again = await builder.run(session, providers, market, as_of=AS_OF)
    assert again.rejected_by_reason[s1.REASON_LEAD_SUPPRESSED] == 1
    assert again.queued == 4
    assert lead.status is SellerLeadStatus.SUPPRESSED


async def test_market_deal_band_overrides_the_global_band(
    session: AsyncSession, providers: DataProviders
) -> None:
    market = Market(
        name="Houston",
        state="TX",
        tier=MarketTier.ACTIVE,
        deal_band_min=Decimal("200000"),
        deal_band_max=Decimal("350000"),
    )
    session.add(market)
    await session.flush()
    result = await S1SellerListBuilder().run(session, providers, market, as_of=AS_OF)
    # Only the $210k and $240k parcels remain inside the narrower band.
    assert result.queued == 2
    assert result.rejected_by_reason[s1.REASON_OUTSIDE_BAND] == 5


def test_market_ref_label_matches_fixture_keys() -> None:
    assert MarketRef("Houston", "TX").label == "Houston, TX"
