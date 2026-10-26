"""D2 property matcher (BB-07..BB-09, BB-12): dedupe, estimates, ranking, flags, evidence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from wholesaling.agents.a7.comps import tiers_from_config
from wholesaling.agents.a7.templates import check_no_agreement_claim
from wholesaling.agents.d2 import (
    SOURCE_A7_CONTRACT,
    WEIGHTS,
    CandidateEstimate,
    D2PropertyMatcher,
    estimate_candidate,
    property_class_of,
)
from wholesaling.config.tunables import Tunables, get_tunables
from wholesaling.db.models.enums import ArvConfidence, ConditionClass
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    ActiveListing,
    ListingSearch,
    MockPropertyDataProvider,
    PropertyAddress,
    mock_property_provider,
)

CRITERIA: dict[str, Any] = {
    "geography": {
        "markets": ["Houston, TX"],
        "cities": [],
        "zips": ["77008", "77018", "77055", "77007"],
        "neighborhoods": [],
    },
    "property_classes": ["sfr"],
    "price_min": "100000",
    "price_max": "250000",
    "condition_tolerance": "heavy",
}


@pytest.fixture
def provider() -> MockPropertyDataProvider:
    return mock_property_provider()


@pytest.fixture
def tunables() -> Tunables:
    return get_tunables()


async def _estimates(
    provider: MockPropertyDataProvider, tunables: Tunables, listings: list[ActiveListing]
) -> list[CandidateEstimate]:
    tiers = tiers_from_config(tunables.a7)
    return [
        await estimate_candidate(
            li,
            provider=provider,
            config=tunables.a7,
            tiers=tiers,
            as_of=DEFAULT_TODAY,
            market_name="Houston",
        )
        for li in listings
    ]


def test_weights_sum_to_one() -> None:
    assert sum(WEIGHTS.values()) == Decimal("1.00")


def test_dedupe_by_parcel_then_address(provider: MockPropertyDataProvider) -> None:
    kept, dropped = D2PropertyMatcher.dedupe(provider.listings)
    assert [d["listing_id"] for d in dropped] == ["L-009"] and dropped[0]["duplicate_of"] == "L-002"
    assert "L-009" not in {k.listing_id for k in kept}
    a = ActiveListing(
        "x1", "s1", PropertyAddress("10 Main Street", "Houston", "TX", "77008"), None, Decimal(1)
    )
    b = ActiveListing(
        "x2", "s2", PropertyAddress("10 MAIN ST", "houston", "tx", "77008"), None, Decimal(1)
    )
    kept2, dropped2 = D2PropertyMatcher.dedupe([a, b])
    assert [k.listing_id for k in kept2] == ["x1"] and dropped2[0]["duplicate_of"] == "x1"


async def test_estimates_are_separate_and_from_a7_arithmetic(
    provider: MockPropertyDataProvider, tunables: Tunables
) -> None:
    li = next(
        li for li in provider.listings if li.listing_id == "L-002"
    )  # 1600 sqft, 77008, medium
    est = (await _estimates(provider, tunables, [li]))[0]
    assert est.arv is not None and est.arv.tier == 1 and est.comp_count == 6
    assert est.repairs_total == Decimal(30) * 1600  # Medium rate x sqft, no line items
    assert est.fees == Decimal(10000)
    assert est.mao is not None and est.assignment_spread == est.mao.mao - Decimal(199000)
    assert est.liquidity_score == Decimal("0.6")  # 6 tier-1 comps / 10
    assert est.flags == [] and est.missing_fields == []


async def test_flags_for_hoa_title_financing_multi_unit_land_and_gaps(
    provider: MockPropertyDataProvider, tunables: Tunables
) -> None:
    by_id = {li.listing_id: li for li in provider.listings}
    ests = {
        e.listing.listing_id: e
        for e in await _estimates(
            provider,
            tunables,
            [by_id["L-003"], by_id["L-004"], by_id["L-005"], by_id["L-006"], by_id["L-007"]],
        )
    }
    assert "hoa" in ests["L-003"].flags
    assert "financing_constraint:cash only" in ests["L-004"].flags
    assert {"title_liens", "data_gap:sqft", "no_arv_estimate"} <= set(ests["L-005"].flags)
    assert ests["L-005"].arv is None and ests["L-005"].repairs_total is None
    assert "multi_unit" in ests["L-006"].flags
    assert {"non_residential", "data_gap:beds", "data_gap:year_built"} <= set(ests["L-007"].flags)
    assert (
        property_class_of(by_id["L-007"]) == "land"
        and property_class_of(by_id["L-006"]) == "small_multi"
    )


async def test_ranking_applies_hard_filters_and_orders_by_score(
    provider: MockPropertyDataProvider, tunables: Tunables
) -> None:
    kept, _ = D2PropertyMatcher.dedupe(await provider.active_listings(ListingSearch()))
    ests = await _estimates(provider, tunables, kept)
    matcher = D2PropertyMatcher(as_of=DEFAULT_TODAY)
    ranked = matcher.rank(CRITERIA, ests)
    ids = [m.estimate.listing.listing_id for m in ranked]
    assert "L-008" not in ids  # Dallas: outside geography
    assert "L-006" not in ids and "L-007" not in ids  # duplex / land: not sfr
    assert "L-004" in ids  # 275k is 10% over the 250k max: inside the 25% slack
    assert ids[0] == "L-001"  # cheapest heavy sfr in 77008 with the widest spread
    scores = [m.score for m in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(
        Decimal(0) <= m.score <= Decimal(1) and Decimal(0) <= m.confidence <= Decimal(1)
        for m in ranked
    )


async def test_price_slack_and_condition_filters(
    provider: MockPropertyDataProvider, tunables: Tunables
) -> None:
    by_id = {li.listing_id: li for li in provider.listings}
    ests = await _estimates(provider, tunables, [by_id["L-004"], by_id["L-005"]])
    matcher = D2PropertyMatcher(as_of=DEFAULT_TODAY)
    # L-004 at 275k is 10% over a 250k max: inside the 25% slack -> kept with a reduced price fit.
    m4 = matcher.score(CRITERIA, ests[0])
    assert m4 is not None and m4.components["price_fit"] == Decimal("0.6")
    # Beyond the slack it is excluded.
    assert matcher.score({**CRITERIA, "price_max": "200000"}, ests[0]) is None
    # L-005 is gut; a heavy tolerance excludes it, a gut tolerance keeps it.
    assert matcher.score(CRITERIA, ests[1]) is None
    assert matcher.score({**CRITERIA, "condition_tolerance": "gut"}, ests[1]) is not None


async def test_evidence_carries_every_bb_08_field_and_never_claims_agreement(
    provider: MockPropertyDataProvider, tunables: Tunables
) -> None:
    li = next(li for li in provider.listings if li.listing_id == "L-001")
    est = (await _estimates(provider, tunables, [li]))[0]
    m = D2PropertyMatcher(as_of=DEFAULT_TODAY).score(CRITERIA, est)
    assert m is not None
    ev = m.evidence
    for key in (
        "source",
        "retrieved_at",
        "match_logic",
        "missing_fields",
        "source_restrictions",
        "freshness",
        "valuation_uncertainty",
        "next_review_action",
        "reason",
        "seller_intent",
    ):
        assert key in ev
    assert ev["review_status"] == "pending" and ev["source"] == "provider"
    assert ev["estimates"]["fees"] == "10000" and ev["match_logic"]["weights"]["spread"] == "0.25"
    assert ev["seller_intent"].startswith("unknown")
    for text in (ev["reason"], ev["seller_intent"], ev["next_review_action"]):
        assert check_no_agreement_claim(text) == []
    assert "no contact made" in ev["seller_intent"]


async def test_off_market_candidate_uses_a7_numbers_and_sel_11_wording(
    provider: MockPropertyDataProvider, tunables: Tunables
) -> None:
    li = ActiveListing(
        listing_id="offer-1",
        source=SOURCE_A7_CONTRACT,
        address=PropertyAddress("1204 Oak Hollow Dr", "Houston", "TX", "77008"),
        parcel_id="HC-0001",
        list_price=Decimal(88500),
        sqft=1600,
        year_built=1985,
        condition_hint="medium",
        retrieved_at=DEFAULT_TODAY,
    )
    est = await estimate_candidate(
        li,
        provider=provider,
        config=tunables.a7,
        tiers=tiers_from_config(tunables.a7),
        as_of=DEFAULT_TODAY,
        market_name="Houston",
        known_arv=Decimal(240161),
        known_repairs=Decimal(60000),
    )
    assert est.is_off_market and est.mao is not None and est.mao.mao == Decimal("95169.32")
    assert est.assignment_spread == Decimal("6669.32")
    m = D2PropertyMatcher(as_of=DEFAULT_TODAY).score(CRITERIA, est)
    assert m is not None and "off-market from A7" in m.reason
    assert (
        "human signature" in m.evidence["seller_intent"]
        and check_no_agreement_claim(m.evidence["seller_intent"]) == []
    )


def test_confidence_scales_with_completeness_and_arv_confidence() -> None:
    li = ActiveListing(
        "x",
        "provider",
        PropertyAddress("1 A St", "Houston", "TX", "77008"),
        None,
        Decimal(150000),
        sqft=None,
        year_built=None,
        condition_hint=None,
        beds=None,
        baths=None,
    )
    est = CandidateEstimate(
        li,
        None,
        None,
        ConditionClass.MEDIUM,
        None,
        Decimal(10000),
        None,
        Decimal(0),
        0,
        None,
        flags=[],
        missing_fields=["sqft", "year_built", "condition_hint", "beds", "baths", "parcel_id"],
    )
    m = D2PropertyMatcher(as_of=DEFAULT_TODAY).score({**CRITERIA, "property_classes": []}, est)
    assert m is not None
    # completeness 1 - 6/10 = 0.4; no ARV -> Low factor 0.40 -> 0.16
    assert m.confidence == Decimal("0.1600") and m.components["spread"] == Decimal("0.3")
    assert m.evidence["valuation_uncertainty"]["arv_confidence"] == "none"
    assert ArvConfidence.LOW.value == "Low"
