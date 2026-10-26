"""Step 2 comp pull (A7-03): tier filters, expansion only when fewer than 3, exclusions."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.agents.a7.conftest import CLEAN, TIER2
from wholesaling.agents.a7.comps import (
    MIN_COMPS,
    CompTier,
    build_query,
    pull_comps,
    tier_by_number,
    tiers_from_config,
)
from wholesaling.config.tunables import A7Config
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    CompQuery,
    MockPropertyDataProvider,
    SoldComp,
)


def _comp(cid: str, *, zip_="77008", sqft=1600, year=1985, days=30, miles="0.3") -> SoldComp:
    return SoldComp(
        comp_id=cid,
        parcel_id=None,
        address=f"{cid} St",
        postal_code=zip_,
        sale_price=Decimal(240000),
        sale_date=DEFAULT_TODAY - timedelta(days=days),
        sqft=sqft,
        distance_miles=Decimal(miles),
        year_built=year,
    )


def test_tiers_from_config_match_a7_03(a7_config: A7Config) -> None:
    tiers = tiers_from_config(a7_config)
    assert [(t.tier, str(t.max_miles), t.max_days) for t in tiers] == [
        (1, "0.75", 180),
        (2, "1.0", 270),
        (3, "1.5", 365),
    ]
    assert tier_by_number(tiers, 2).max_days == 270
    with pytest.raises(KeyError):
        tier_by_number(tiers, 9)


def test_tiers_from_config_requires_at_least_one_tier(a7_config: A7Config) -> None:
    bad = a7_config.model_copy(update={"comp_tiers": []})
    with pytest.raises(ValueError, match="at least one tier"):
        tiers_from_config(bad)


def test_query_filters_zip_sqft_year_age_and_distance() -> None:
    q = CompQuery(
        postal_code="77008",
        subject_sqft=1600,
        subject_year_built=1985,
        max_miles=Decimal("0.75"),
        max_days=180,
        as_of=DEFAULT_TODAY,
    )
    assert q.accepts(_comp("ok"))
    assert not q.accepts(_comp("zip", zip_="77009"))
    assert not q.accepts(_comp("small", sqft=1279))  # below -20%
    assert q.accepts(_comp("edge_lo", sqft=1280))
    assert q.accepts(_comp("edge_hi", sqft=1920))
    assert not q.accepts(_comp("big", sqft=1921))
    assert not q.accepts(_comp("old", year=1969))  # 16 years
    assert q.accepts(_comp("year_edge", year=1970))
    assert not q.accepts(_comp("stale", days=181))
    assert q.accepts(_comp("age_edge", days=180))
    assert not q.accepts(_comp("far", miles="0.76"))
    assert not q.accepts(_comp("future", days=-1))


def test_query_ignores_year_when_unknown() -> None:
    q = CompQuery("77008", 1600, None, Decimal("0.75"), 180, DEFAULT_TODAY)
    assert q.accepts(_comp("x", year=1900))


def test_build_query_uses_tier_limits() -> None:
    tier = CompTier(2, Decimal("1.0"), 270)
    q = build_query(
        postal_code="77008",
        parcel_id="P",
        subject_sqft=1600,
        subject_year_built=1985,
        tier=tier,
        as_of=DEFAULT_TODAY,
    )
    assert q.max_miles == Decimal("1.0") and q.max_days == 270 and q.parcel_id == "P"
    assert q.sqft_tolerance_pct == Decimal("0.20") and q.year_tolerance == 15


async def test_clean_subject_stays_at_tier_1_with_seven_comps(
    provider: MockPropertyDataProvider, a7_config: A7Config
) -> None:
    pull = await pull_comps(
        provider,
        postal_code="77008",
        parcel_id="HC-0001",
        subject_sqft=1600,
        subject_year_built=1985,
        tiers=tiers_from_config(a7_config),
        as_of=DEFAULT_TODAY,
    )
    assert pull.tier.tier == 1 and pull.count == 7
    assert len(pull.attempts) == 1 and pull.attempts[0]["returned"] == 7


async def test_expands_to_tier_2_only_when_fewer_than_three(
    provider: MockPropertyDataProvider, a7_config: A7Config
) -> None:
    pull = await pull_comps(
        provider,
        postal_code=TIER2.postal_code or "",
        parcel_id="HC-0002",
        subject_sqft=1400,
        subject_year_built=1978,
        tiers=tiers_from_config(a7_config),
        as_of=DEFAULT_TODAY,
    )
    assert pull.tier.tier == 2 and pull.count == 5
    assert [a["tier"] for a in pull.attempts] == [1, 2]
    assert pull.attempts[0]["after_exclusions"] == 2 < MIN_COMPS


async def test_exclusions_can_force_expansion_and_are_recorded(
    provider: MockPropertyDataProvider, a7_config: A7Config
) -> None:
    pull = await pull_comps(
        provider,
        postal_code=CLEAN.postal_code or "",
        parcel_id="HC-0001",
        subject_sqft=1600,
        subject_year_built=1985,
        tiers=tiers_from_config(a7_config),
        as_of=DEFAULT_TODAY,
        exclude_ids=["CL1", "CL2", "CL3", "CL4", "CL5"],
    )
    # 2 remain at tier 1; tiers 2 and 3 add nothing for this subject -> last tier used, short.
    assert pull.tier.tier == 3 and pull.count == 2
    assert pull.excluded_ids == ["CL1", "CL2", "CL3", "CL4", "CL5"]
    assert [a["tier"] for a in pull.attempts] == [1, 2, 3]


async def test_start_tier_skips_lower_tiers(
    provider: MockPropertyDataProvider, a7_config: A7Config
) -> None:
    pull = await pull_comps(
        provider,
        postal_code="77008",
        parcel_id="HC-0001",
        subject_sqft=1600,
        subject_year_built=1985,
        tiers=tiers_from_config(a7_config),
        as_of=DEFAULT_TODAY,
        start_tier=2,
    )
    assert pull.tier.tier == 2 and [a["tier"] for a in pull.attempts] == [2]
    with pytest.raises(ValueError, match="no comp tier"):
        await pull_comps(
            provider,
            postal_code="77008",
            parcel_id="HC-0001",
            subject_sqft=1600,
            subject_year_built=1985,
            tiers=tiers_from_config(a7_config),
            as_of=DEFAULT_TODAY,
            start_tier=4,
        )


async def test_advance_clock_ages_comps_out_of_tier_1(
    provider: MockPropertyDataProvider, a7_config: A7Config
) -> None:
    provider.advance_clock(170)  # only CL7 (10 d -> 180 d) is still inside tier 1
    pull = await pull_comps(
        provider,
        postal_code="77008",
        parcel_id="HC-0001",
        subject_sqft=1600,
        subject_year_built=1985,
        tiers=tiers_from_config(a7_config),
        as_of=DEFAULT_TODAY,
    )
    assert provider.today == date(2026, 10, 25) + timedelta(days=170)
    assert [a["after_exclusions"] for a in pull.attempts] == [1, 5]
    assert pull.tier.tier == 2 and pull.count == 5
    # Ordered by distance then date: CL7 0.1 mi, CL3 0.2, CL1 0.3, CL5 0.4, CL2 0.5.
    assert pull.to_json()["comp_ids"] == ["CL7", "CL3", "CL1", "CL5", "CL2"]
