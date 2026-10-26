"""Step 3 ARV (A7-04, ENG-Q10): trim, linear decay weights, weighted mean, confidence.

The clean-fixture expectation is computed by hand in the docstring of
``test_clean_fixture_arv_matches_hand_computation`` and asserted to the dollar.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.agents.a7.conftest import CLEAN
from wholesaling.agents.a7.arv import (
    compute_arv,
    confidence_for,
    linear_decay,
    lower_confidence,
    pct_gap,
    trim_count,
)
from wholesaling.agents.a7.comps import CompTier
from wholesaling.db.models.enums import ArvConfidence
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    MockPropertyDataProvider,
    SoldComp,
)

TIER1 = CompTier(1, Decimal("0.75"), 180)
TIER2 = CompTier(2, Decimal("1.0"), 270)


def _comp(cid: str, ppsf: int, sqft: int, days: int, miles: str) -> SoldComp:
    return SoldComp(
        comp_id=cid,
        parcel_id=None,
        address=cid,
        postal_code="77008",
        sale_price=Decimal(ppsf * sqft),
        sale_date=DEFAULT_TODAY - timedelta(days=days),
        sqft=sqft,
        distance_miles=Decimal(miles),
    )


@pytest.mark.parametrize(
    ("n", "k"),
    [(1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (6, 0), (7, 1), (10, 1), (13, 1), (14, 2), (20, 3)],
)
def test_trim_count_is_floor_15pct_each_end_keeping_three(n: int, k: int) -> None:
    assert trim_count(n) == k
    assert n - 2 * trim_count(n) >= min(n, 3)


def test_linear_decay_runs_from_one_to_half_at_the_limit() -> None:
    assert linear_decay(Decimal(0), Decimal(180)) == Decimal("1.000000")
    assert linear_decay(Decimal(90), Decimal(180)) == Decimal("0.750000")
    assert linear_decay(Decimal(180), Decimal(180)) == Decimal("0.500000")
    assert linear_decay(Decimal(400), Decimal(180)) == Decimal("0.500000")  # clamped
    assert linear_decay(Decimal("0.375"), Decimal("0.75")) == Decimal("0.750000")
    assert linear_decay(Decimal(5), Decimal(0)) == Decimal(1)


@pytest.mark.parametrize(
    ("count", "tier", "spread", "expected"),
    [
        (5, 1, "0.12", ArvConfidence.HIGH),
        (7, 1, "0.0999", ArvConfidence.HIGH),
        (4, 1, "0.05", ArvConfidence.MEDIUM),  # fewer than 5 comps
        (5, 2, "0.05", ArvConfidence.MEDIUM),  # tier 2
        (5, 1, "0.1201", ArvConfidence.MEDIUM),  # spread just over 12%
        (3, 2, "0.20", ArvConfidence.MEDIUM),
        (2, 1, "0.01", ArvConfidence.LOW),  # fewer than 3
        (5, 3, "0.05", ArvConfidence.LOW),  # tier 3
        (6, 2, "0.2001", ArvConfidence.LOW),  # spread over 20%
    ],
)
def test_confidence_rules(count: int, tier: int, spread: str, expected: ArvConfidence) -> None:
    confidence, reason = confidence_for(count, tier, Decimal(spread))
    assert confidence is expected
    assert f"{count} comps" in reason and f"tier {tier}" in reason


def test_lower_confidence_steps_down_and_floors_at_low() -> None:
    assert lower_confidence(ArvConfidence.HIGH) is ArvConfidence.MEDIUM
    assert lower_confidence(ArvConfidence.MEDIUM) is ArvConfidence.LOW
    assert lower_confidence(ArvConfidence.LOW) is ArvConfidence.LOW


def test_pct_gap_relative_to_larger_magnitude() -> None:
    assert pct_gap(Decimal(100), Decimal(90)) == Decimal("0.1000")
    assert pct_gap(Decimal(90), Decimal(100)) == Decimal("0.1000")
    assert pct_gap(Decimal(0), Decimal(0)) == Decimal(0)


def test_compute_arv_rejects_empty_set_and_bad_sqft() -> None:
    with pytest.raises(ValueError, match="without comps"):
        compute_arv([], subject_sqft=1500, tier=TIER1, as_of=DEFAULT_TODAY)
    with pytest.raises(ValueError, match="positive"):
        compute_arv(
            [_comp("a", 150, 1500, 10, "0.1")], subject_sqft=0, tier=TIER1, as_of=DEFAULT_TODAY
        )


def test_single_comp_is_its_own_price_per_sqft() -> None:
    est = compute_arv(
        [_comp("a", 150, 1500, 10, "0.1")], subject_sqft=1600, tier=TIER1, as_of=DEFAULT_TODAY
    )
    assert est.arv == Decimal(240000) and est.spread_pct == 0
    assert est.confidence is ArvConfidence.LOW  # 1 comp
    assert est.kept_count == 1 and est.trimmed_ids == []


def test_clean_fixture_arv_matches_hand_computation(provider: MockPropertyDataProvider) -> None:
    """Seven tier-1 comps at 140/145/148/150/155/160/162 $/sqft; trim 1 each end (140, 162).

    weight = recency x distance, linear 1.0 -> 0.5 at 180 d / 0.75 mi:
        CL3 145: 20 d, 0.20 mi -> 0.944444 x 0.866667 = 0.818518
        CL5 148: 60 d, 0.40 mi -> 0.833333 x 0.733333 = 0.611111
        CL1 150: 40 d, 0.30 mi -> 0.888889 x 0.800000 = 0.711111
        CL2 155: 90 d, 0.50 mi -> 0.750000 x 0.666667 = 0.500000
        CL4 160: 150 d, 0.70 mi -> 0.583333 x 0.533333 = 0.311111
    sum w = 2.951851; sum w*p = 443.0738 -> weighted mean 150.1004 $/sqft
    ARV = 150.1004 x 1600 = 240,160.64 -> $240,161. Spread = (160-145)/150.1004 = 9.99%.
    """
    subject = provider.subject(CLEAN)
    assert subject is not None
    est = compute_arv(subject.comps, subject_sqft=1600, tier=TIER1, as_of=DEFAULT_TODAY)
    assert est.trimmed_ids == ["CL6", "CL7"]
    assert est.kept_ids == ["CL3", "CL5", "CL1", "CL2", "CL4"]
    by_id = {w.comp_id: w for w in est.comps}
    assert by_id["CL3"].weight == Decimal("0.818518")
    assert by_id["CL5"].weight == Decimal("0.611111")
    assert by_id["CL1"].weight == Decimal("0.711111")
    assert by_id["CL2"].weight == Decimal("0.500000")
    assert by_id["CL4"].weight == Decimal("0.311111")
    assert est.weighted_price_per_sqft == Decimal("150.1004")
    assert est.arv == Decimal(240161)
    assert est.spread_pct == Decimal("0.0999")
    assert est.confidence is ArvConfidence.HIGH
    assert est.comp_count == 7 and est.kept_count == 5


def test_weights_pull_the_mean_towards_recent_and_close_comps() -> None:
    comps = [
        _comp("near_new", 200, 1500, 0, "0"),  # weight 1.0
        _comp("far_old", 100, 1500, 180, "0.75"),  # weight 0.25
        _comp("mid", 150, 1500, 90, "0.375"),  # weight 0.5625
    ]
    est = compute_arv(comps, subject_sqft=1000, tier=TIER1, as_of=DEFAULT_TODAY)
    # (200*1 + 100*0.25 + 150*0.5625) / 1.8125 = 309.375 / 1.8125 = 170.6897
    assert est.weighted_price_per_sqft == Decimal("170.6897")
    assert est.arv == Decimal(170690)


def test_tier_limits_change_the_weights() -> None:
    comps = [_comp(f"c{i}", 150, 1500, 90, "0.5") for i in range(3)]
    t1 = compute_arv(comps, subject_sqft=1500, tier=TIER1, as_of=DEFAULT_TODAY)
    t2 = compute_arv(comps, subject_sqft=1500, tier=TIER2, as_of=DEFAULT_TODAY)
    assert t1.comps[0].weight == Decimal("0.500000")  # 0.75 x 0.666667
    assert t2.comps[0].weight == Decimal("0.625000")  # 0.833333 x 0.75
    assert t1.arv == t2.arv == Decimal(225000)  # equal weights -> same mean
    assert t1.tier == 1 and t2.tier == 2


def test_to_json_carries_method_and_every_comp() -> None:
    comps = [_comp(f"c{i}", 150 + i, 1500, 10 * i, "0.1") for i in range(7)]
    est = compute_arv(comps, subject_sqft=1500, tier=TIER1, as_of=DEFAULT_TODAY)
    doc = est.to_json()
    assert "ENG-Q10" in doc["method"]
    assert len(doc["comps"]) == 7 and sum(c["trimmed"] for c in doc["comps"]) == 2
    assert doc["confidence"] == "High"
