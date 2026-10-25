"""Table-driven tests for agents/scoring.py, one block per REQUIREMENTS.md row."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tests.agents.conftest import AS_OF_DATE, make_deed
from wholesaling.agents import scoring
from wholesaling.agents.scoring import (
    EntityCandidate,
    InvestorFeatures,
    MarketScore,
    PurchaseEvent,
    RecordSet,
    classify_investor_type,
    deal_band_for_market,
    investor_score,
    is_excluded,
    market_score,
    qualifies,
    tier_markets,
)
from wholesaling.config.tunables import DealBand, M1Weights
from wholesaling.db.models.enums import InvestorType, MarketTier
from wholesaling.integrations.data.models import (
    DistressSignals,
    MarketRef,
    MarketSignals,
    ReferenceLists,
)

WEIGHTS = M1Weights()
GLOBAL_BAND = DealBand()
BAND = (GLOBAL_BAND.min_purchase, GLOBAL_BAND.max_purchase)
MKT = MarketRef("Houston", "TX")


def _signals(**kw: object) -> MarketSignals:
    base: dict[str, object] = {"market": MKT, "data_available": True}
    base.update(kw)
    return MarketSignals(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- M1-02 / M1-03


def test_m1_03_formula_with_saturated_signals_equals_sum_of_positive_weights() -> None:
    sig = _signals(
        entity_purchase_share=1.0,
        cash_share=1.0,
        repeat_buyer_count=1000,
        i2i_resale_share=1.0,
        cash_vs_retail_spread=1.0,
        flip_profit_median=Decimal("100000"),
        distress=DistressSignals(50, 50, 50, 50, 50),
        days_on_market=0,
        months_inventory=0,
        sold_trend=1.0,
        share_in_band=1.0,
    )
    res = market_score(sig, WEIGHTS, friction=0)
    assert res.gates_passed
    assert res.score == pytest.approx(0.30 + 0.25 + 0.20 + 0.10 + 0.10)
    assert res.contributions["friction"] == 0


def test_m1_02_friction_is_negative_and_scaled_0_to_3() -> None:
    sig = _signals(share_in_band=1.0)
    s0 = market_score(sig, WEIGHTS, friction=0).score
    s1 = market_score(sig, WEIGHTS, friction=1).score
    s3 = market_score(sig, WEIGHTS, friction=3)
    assert s0 is not None and s1 is not None
    assert s0 - s1 == pytest.approx(0.05 / 3, abs=1e-5)
    assert s3.legal_gate_passed is False  # M1-04 legal gate


def test_m1_02_data_availability_is_a_gate_not_a_weight() -> None:
    res = market_score(_signals(data_available=False, share_in_band=1.0), WEIGHTS, friction=0)
    assert res.data_gate_passed is False
    assert res.score is None
    assert res.components == {}


def test_m1_02_missing_friction_entry_scores_as_zero_but_flags_legal_review() -> None:
    res = market_score(_signals(share_in_band=1.0), WEIGHTS, friction=None)
    assert res.legal_review_pending is True
    assert res.legal_gate_passed is True
    assert res.contributions["friction"] == 0


def test_m1_03_components_are_clamped_to_unit_interval() -> None:
    sig = _signals(entity_purchase_share=5.0, days_on_market=10_000, share_in_band=7.0)
    res = market_score(sig, WEIGHTS, friction=0)
    assert all(0.0 <= v <= 1.0 for v in res.components.values())


def test_m1_03_contributions_sum_to_score() -> None:
    sig = _signals(
        entity_purchase_share=0.2,
        cash_share=0.3,
        repeat_buyer_count=50,
        cash_vs_retail_spread=0.15,
        distress=DistressSignals(3, 4, 1, 8, 2),
        days_on_market=40,
        months_inventory=3,
        sold_trend=0.02,
        share_in_band=0.5,
    )
    res = market_score(sig, WEIGHTS, friction=1)
    assert res.score == pytest.approx(sum(res.contributions.values()), abs=1e-6)


# ---------------------------------------------------------------------------- M1-04 / M1-07


def _ms(
    score: float | None, *, data: bool = True, legal: bool = True, pending: bool = False
) -> MarketScore:
    return MarketScore(
        data_gate_passed=data,
        legal_gate_passed=legal,
        legal_review_pending=pending,
        friction=None if pending else 1,
        score=score,
    )


def test_m1_04_new_markets_start_in_watch_never_active() -> None:
    scores = {"A": _ms(0.9), "B": _ms(0.8)}
    out = tier_markets(scores, 5, 10, current_tiers={}, buy_box_counts={"A": 50}, crossings={})
    assert out["A"].new is MarketTier.WATCH
    assert out["A"].reason == "new_market_starts_in_watch"
    assert out["A"].rank == 1 and out["B"].rank == 2


def test_m1_04_failed_gates_are_excluded_immediately() -> None:
    scores = {"D": _ms(None, data=False), "L": _ms(0.9, legal=False)}
    out = tier_markets(
        scores,
        5,
        10,
        current_tiers={"D": MarketTier.ACTIVE, "L": MarketTier.WATCH},
        buy_box_counts={},
        crossings={"D": 1},
    )
    assert out["D"].new is MarketTier.EXCLUDED and out["D"].reason == "data_gate_failed"
    assert out["L"].new is MarketTier.EXCLUDED and out["L"].reason == "legal_gate_failed"
    assert out["D"].crossings == 0


def test_m1_04_watch_to_active_requires_ten_buy_boxes() -> None:
    scores = {"A": _ms(0.9)}
    few = tier_markets(scores, 5, 10, {"A": MarketTier.WATCH}, {"A": 9}, {})
    assert few["A"].proposed is MarketTier.WATCH
    assert few["A"].reason == "below_buy_box_threshold"
    enough = tier_markets(scores, 5, 10, {"A": MarketTier.WATCH}, {"A": 10}, {})
    assert enough["A"].proposed is MarketTier.ACTIVE


def test_m1_07_promotion_takes_effect_only_on_second_crossing() -> None:
    scores = {"A": _ms(0.9)}
    first = tier_markets(scores, 5, 10, {"A": MarketTier.WATCH}, {"A": 12}, {})
    assert first["A"].proposed is MarketTier.ACTIVE
    assert first["A"].new is MarketTier.WATCH
    assert first["A"].crossings == 1
    assert first["A"].reason.endswith("_pending_second_crossing")
    second = tier_markets(
        scores, 5, 10, {"A": MarketTier.WATCH}, {"A": 12}, {"A": first["A"].crossings}
    )
    assert second["A"].new is MarketTier.ACTIVE
    assert second["A"].crossings == 0


def test_m1_07_crossing_counter_resets_when_proposal_agrees_again() -> None:
    scores = {"A": _ms(0.9)}
    back_to_normal = tier_markets(scores, 5, 10, {"A": MarketTier.WATCH}, {"A": 3}, {"A": 1})
    assert back_to_normal["A"].new is MarketTier.WATCH
    assert back_to_normal["A"].crossings == 0


def test_m1_07_demotion_also_requires_two_crossings() -> None:
    scores = {"A": _ms(0.9), "B": _ms(0.8)}
    first = tier_markets(
        scores, 1, 10, {"A": MarketTier.ACTIVE, "B": MarketTier.ACTIVE}, {"A": 20, "B": 20}, {}
    )
    assert first["B"].proposed is MarketTier.WATCH and first["B"].new is MarketTier.ACTIVE
    assert first["B"].reason.startswith("outside_active_cap")
    second = tier_markets(
        scores,
        1,
        10,
        {"A": MarketTier.ACTIVE, "B": MarketTier.ACTIVE},
        {"A": 20, "B": 20},
        {"B": 1},
    )
    assert second["B"].new is MarketTier.WATCH


def test_m1_04_active_cap_limits_promotions_by_rank() -> None:
    scores = {m: _ms(s) for m, s in (("A", 0.9), ("B", 0.8), ("C", 0.7), ("D", 0.6))}
    tiers = {m: MarketTier.WATCH for m in scores}
    boxes = {m: 50 for m in scores}
    out = tier_markets(scores, 3, 10, tiers, boxes, {})
    assert [out[m].proposed for m in "ABC"] == [MarketTier.ACTIVE] * 3
    assert out["D"].proposed is MarketTier.WATCH and out["D"].reason == "outside_active_cap"


def test_m1_04_legal_review_pending_blocks_promotion() -> None:
    scores = {"A": _ms(0.9, pending=True)}
    out = tier_markets(scores, 5, 10, {"A": MarketTier.WATCH}, {"A": 50}, {})
    assert out["A"].proposed is MarketTier.WATCH
    assert out["A"].reason == "legal_review_pending"


def test_m1_04_excluded_market_whose_gates_pass_returns_to_watch_not_active() -> None:
    scores = {"A": _ms(0.9)}
    out = tier_markets(scores, 5, 10, {"A": MarketTier.EXCLUDED}, {"A": 50}, {"A": 1})
    assert out["A"].new is MarketTier.WATCH
    assert out["A"].crossings == 0


# ---------------------------------------------------------------------------- M1-06


@pytest.mark.parametrize(
    ("median", "expected"),
    [
        (None, (Decimal("75000"), Decimal("350000"))),
        (Decimal("200000"), (Decimal("100000"), Decimal("300000"))),
        (Decimal("262000"), (Decimal("131000"), Decimal("350000"))),  # upper clamp
        (Decimal("120000"), (Decimal("75000"), Decimal("180000"))),  # lower clamp
        (Decimal("900000"), (Decimal("350000"), Decimal("350000"))),  # both clamp to the top
        (Decimal("40000"), (Decimal("75000"), Decimal("75000"))),  # both clamp to the bottom
        (Decimal("0"), (Decimal("75000"), Decimal("350000"))),
    ],
)
def test_m1_06_deal_band_is_half_to_one_and_a_half_median_clamped(
    median: Decimal | None, expected: tuple[Decimal, Decimal]
) -> None:
    assert deal_band_for_market(median, GLOBAL_BAND) == expected


# ---------------------------------------------------------------------------- INV-02


def _rs(records: list, *, is_entity: bool = True) -> RecordSet:  # type: ignore[type-arg]
    return RecordSet(is_entity=is_entity, purchases=records, band=BAND, as_of=AS_OF_DATE)


def test_inv_02_entity_with_one_cash_in_band_recent_purchase_qualifies() -> None:
    q = qualifies(_rs([make_deed("Acme LLC")]))
    assert q.qualified and q.reason is None
    assert len(q.qualifying_purchases) == 1


def test_inv_02_individual_needs_two_purchases_in_24_months() -> None:
    one = qualifies(_rs([make_deed("Jane Doe", entity_type="individual")], is_entity=False))
    assert not one.qualified and one.reason == "individual_below_two_purchases"
    two = qualifies(
        _rs(
            [
                make_deed("Jane Doe", months_ago=20, entity_type="individual"),
                make_deed("Jane Doe", months_ago=2, entity_type="individual"),
            ],
            is_entity=False,
        )
    )
    assert two.qualified


@pytest.mark.parametrize("financing", ["conventional", "unknown"])
def test_inv_02_conventional_or_unknown_financing_is_not_cash(financing: str) -> None:
    q = qualifies(_rs([make_deed("Acme LLC", financing=financing)]))
    assert not q.qualified and q.reason == "no_cash_purchase"


def test_inv_02_hard_money_counts_as_cash_like() -> None:
    assert qualifies(_rs([make_deed("Acme LLC", financing="hard_money")])).qualified


@pytest.mark.parametrize(
    ("ptype", "units"), [("land", 0), ("multifamily", 12), ("other", 1), ("sfr", 5)]
)
def test_inv_02_only_residential_1_to_4_units(ptype: str, units: int) -> None:
    q = qualifies(_rs([make_deed("Acme LLC", property_type=ptype, units=units)]))
    assert not q.qualified and q.reason == "not_residential_1_4"


@pytest.mark.parametrize("price", [74_999, 350_001, 900_000])
def test_inv_02_purchase_outside_band_does_not_qualify(price: int) -> None:
    q = qualifies(_rs([make_deed("Acme LLC", price=price)]))
    assert not q.qualified and q.reason == "outside_deal_band"


@pytest.mark.parametrize("price", [75_000, 350_000])
def test_inv_02_band_is_inclusive(price: int) -> None:
    assert qualifies(_rs([make_deed("Acme LLC", price=price)])).qualified


def test_inv_02_last_purchase_over_18_months_is_stale() -> None:
    q = qualifies(_rs([make_deed("Acme LLC", months_ago=18.5)]))
    assert not q.qualified and q.reason == "stale_last_purchase"
    assert qualifies(_rs([make_deed("Acme LLC", months_ago=17.5)])).qualified


def test_inv_02_one_qualifying_record_is_enough_and_others_are_kept_for_rates() -> None:
    q = qualifies(
        _rs(
            [
                make_deed("Acme LLC", price=900_000),
                make_deed("Acme LLC", financing="conventional"),
                make_deed("Acme LLC", months_ago=3),
            ]
        )
    )
    assert q.qualified
    assert len(q.qualifying_purchases) == 1


def test_inv_02_empty_record_set() -> None:
    assert qualifies(_rs([])).reason == "no_purchases"


# ---------------------------------------------------------------------------- INV-03


def _ev(
    months_ago: float,
    price: int,
    zip_code: str = "77009",
    resale_after: float | None = None,
    resale_price: int | None = None,
) -> PurchaseEvent:
    from datetime import timedelta

    bought = AS_OF_DATE - timedelta(days=round(months_ago * 30.4375))
    resold = (
        None if resale_after is None else bought + timedelta(days=round(resale_after * 30.4375))
    )
    return PurchaseEvent(
        purchase_date=bought,
        price=Decimal(price),
        zip=zip_code,
        resale_date=resold,
        resale_price=None if resale_price is None else Decimal(resale_price),
    )


def test_inv_03_flipper_resells_in_4_to_12_months_at_a_higher_price() -> None:
    hist = [_ev(14, 185_000, resale_after=7, resale_price=265_000), _ev(1, 168_000)]
    assert classify_investor_type(hist, AS_OF_DATE) is InvestorType.FLIPPER


def test_inv_03_resale_at_a_loss_is_not_a_flip() -> None:
    hist = [_ev(14, 185_000, resale_after=7, resale_price=150_000)]
    assert classify_investor_type(hist, AS_OF_DATE) is InvestorType.UNKNOWN


def test_inv_03_buy_and_hold_holds_over_12_months() -> None:
    hist = [
        _ev(22, 95_000, "77021"),
        _ev(19, 110_000, "77021"),
        _ev(16, 120_000, "77021"),
        _ev(4, 128_000, "77021"),
    ]
    assert classify_investor_type(hist, AS_OF_DATE) is InvestorType.BUY_AND_HOLD


def test_inv_03_recent_cluster_without_long_holds_is_unknown() -> None:
    hist = [_ev(9, 200_000, "77007"), _ev(5, 250_000, "77007"), _ev(2, 480_000, "77007")]
    assert classify_investor_type(hist, AS_OF_DATE) is InvestorType.UNKNOWN


def test_inv_03_wholesaler_resells_fast_on_most_purchases() -> None:
    hist = [
        _ev(10, 95_000, resale_after=1, resale_price=118_000),
        _ev(6, 130_000, resale_after=1, resale_price=152_000),
        _ev(2, 140_000),
    ]
    assert classify_investor_type(hist, AS_OF_DATE) is InvestorType.WHOLESALER


def test_inv_03_single_fast_resale_is_not_enough_for_wholesaler() -> None:
    hist = [_ev(10, 95_000, resale_after=1, resale_price=118_000), _ev(6, 130_000), _ev(2, 140_000)]
    assert classify_investor_type(hist, AS_OF_DATE) is InvestorType.UNKNOWN


def test_inv_03_known_entity_list_wins_over_behaviour() -> None:
    hist = [_ev(14, 185_000, resale_after=7, resale_price=265_000)]
    assert (
        classify_investor_type(hist, AS_OF_DATE, known_institutional=True)
        is InvestorType.IBUYER_INSTITUTIONAL
    )


def test_inv_03_licence_match_flags_agent_investor() -> None:
    hist = [_ev(14, 185_000, resale_after=7, resale_price=265_000)]
    assert (
        classify_investor_type(hist, AS_OF_DATE, licensed_agent=True) is InvestorType.AGENT_INVESTOR
    )


def test_inv_03_empty_history_is_unknown() -> None:
    assert classify_investor_type([], AS_OF_DATE) is InvestorType.UNKNOWN


# ---------------------------------------------------------------------------- INV-04


def _features(**kw: object) -> InvestorFeatures:
    base: dict[str, object] = {
        "purchase_count_24m": 6,
        "months_since_last": 0.0,
        "cash_rate": 1.0,
        "band_fit_rate": 1.0,
        "market_concentration": 1.0,
        "investor_type": InvestorType.UNKNOWN,
    }
    base.update(kw)
    return InvestorFeatures(**base)  # type: ignore[arg-type]


def test_inv_04_weights_sum_to_95_plus_type_bonus_at_full_marks() -> None:
    s = investor_score(_features())
    assert s.total == 95  # 30 + 25 + 15 + 15 + 10; the +-5 type bonus reaches 100
    assert investor_score(_features(investor_type=InvestorType.FLIPPER)).total == 100
    assert s.breakdown["volume"] == 30
    assert s.breakdown["recency"] == 25
    assert s.breakdown["cash_rate"] == 15
    assert s.breakdown["band_fit"] == 15
    assert s.breakdown["concentration"] == 10
    assert s.breakdown["type_bonus"] == 0


@pytest.mark.parametrize(
    ("itype", "bonus"),
    [
        (InvestorType.FLIPPER, 5),
        (InvestorType.BUY_AND_HOLD, 5),
        (InvestorType.WHOLESALER, -5),
        (InvestorType.AGENT_INVESTOR, 0),
        (InvestorType.UNKNOWN, 0),
    ],
)
def test_inv_04_type_bonus_is_plus_or_minus_five(itype: InvestorType, bonus: int) -> None:
    s = investor_score(_features(purchase_count_24m=3, investor_type=itype))
    assert s.breakdown["type_bonus"] == bonus
    assert s.total == 80 + bonus  # 15 + 25 + 15 + 15 + 10


def test_inv_04_score_is_clamped_to_0_100() -> None:
    assert investor_score(_features(investor_type=InvestorType.FLIPPER)).total == 100
    assert (
        investor_score(
            _features(
                purchase_count_24m=0,
                months_since_last=30,
                cash_rate=0,
                band_fit_rate=0,
                market_concentration=0,
                investor_type=InvestorType.WHOLESALER,
            )
        ).total
        == 0
    )


def test_inv_04_recency_decays_linearly_to_zero_at_18_months() -> None:
    assert investor_score(_features(months_since_last=9)).breakdown["recency"] == 12.5
    assert investor_score(_features(months_since_last=18)).breakdown["recency"] == 0
    assert investor_score(_features(months_since_last=25)).breakdown["recency"] == 0


def test_inv_04_volume_saturates_at_six_purchases() -> None:
    assert investor_score(_features(purchase_count_24m=3)).breakdown["volume"] == 15
    assert investor_score(_features(purchase_count_24m=12)).breakdown["volume"] == 30


def test_inv_04_prospect_and_priority_thresholds() -> None:
    assert scoring.PROSPECT_THRESHOLD == 50 and scoring.PRIORITY_THRESHOLD == 75
    s49 = investor_score(
        _features(
            purchase_count_24m=1,
            months_since_last=7.92,
            cash_rate=1,
            band_fit_rate=1,
            market_concentration=0,
        )
    )
    assert s49.total == 49 and not s49.is_prospect
    s75 = investor_score(
        _features(
            purchase_count_24m=4,
            months_since_last=0,
            cash_rate=1,
            band_fit_rate=1,
            market_concentration=0,
        )
    )
    assert s75.total == 75 and s75.is_priority


def test_inv_04_houston_fixture_scores_match_hand_calculation() -> None:
    # Bayou Capital Partners: 4 buys, 1 month, all cash, all in band, flipper.
    bayou = investor_score(
        _features(purchase_count_24m=4, months_since_last=1.0, investor_type=InvestorType.FLIPPER)
    )
    assert bayou.total == 89
    # Maria Hernandez: 2 buys, 6 months, individual, unknown type.
    maria = investor_score(_features(purchase_count_24m=2, months_since_last=6.0))
    assert maria.total == 67
    # Fast Close Houston: 3 buys, 2 months, wholesaler.
    fast = investor_score(
        _features(
            purchase_count_24m=3, months_since_last=2.0, investor_type=InvestorType.WHOLESALER
        )
    )
    assert fast.total == 72


# ---------------------------------------------------------------------------- INV-05

LISTS = ReferenceLists(
    institutional=frozenset({"OPENDOOR PROPERTY TRUST I"}),
    licensed_agents=frozenset(),
    suppressed=frozenset({"SUPPRESSED HOLDINGS LLC"}),
)


def _entity(name_key: str, records: list | None = None) -> EntityCandidate:  # type: ignore[type-arg]
    return EntityCandidate(
        entity_keys=frozenset({name_key}), purchases=records or [make_deed(name_key)]
    )


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("OPENDOOR PROPERTY TRUST I", "institutional"),
        ("SUPPRESSED HOLDINGS LLC", "suppressed"),
        ("HARRIS COUNTY", "government"),
        ("CITY OF HOUSTON", "government"),
        ("HOUSTON HOUSING AUTHORITY", "government"),
        ("HABITAT FOR HUMANITY HOUSTON INC", "nonprofit"),
        ("GRACE CHURCH", "nonprofit"),
        ("WELLS FARGO BANK NA", "lender_reo"),
        ("FIRST MORTGAGE CORP", "lender_reo"),
        ("TEXAS DOW EMPLOYEES CREDIT UNION", "lender_reo"),
        ("FANNIE MAE", "lender_reo"),
    ],
)
def test_inv_05_exclusion_classes(key: str, reason: str) -> None:
    assert is_excluded(_entity(key), LISTS) == reason


def test_inv_05_builder_excluded_only_when_buying_lots() -> None:
    lots = [
        make_deed("Lennar Homes of Texas LLC", property_type="land", units=0, ref=f"L{i}")
        for i in range(3)
    ]
    assert is_excluded(_entity("LENNAR HOMES OF TEXAS LLC", lots), LISTS) == "builder_lots"
    houses = [make_deed("Lennar Homes of Texas LLC", ref=f"H{i}") for i in range(3)]
    assert is_excluded(_entity("LENNAR HOMES OF TEXAS LLC", houses), LISTS) is None


def test_inv_05_token_match_not_substring() -> None:
    assert is_excluded(_entity("RIVERBANK HOLDINGS LLC"), LISTS) is None
    assert is_excluded(_entity("STATELINE PROPERTIES LLC"), LISTS) is None
    assert is_excluded(_entity("BAYOU CAPITAL PARTNERS LLC"), LISTS) is None


def test_inv_05_runtime_suppression_set_is_honoured() -> None:
    assert is_excluded(_entity("ACME LLC"), LISTS, suppression={"ACME LLC"}) == "suppressed"


def test_inv_05_any_merged_key_can_trigger_exclusion() -> None:
    cand = EntityCandidate(
        entity_keys=frozenset({"ACME LLC", "OPENDOOR PROPERTY TRUST I"}),
        purchases=[make_deed("Acme LLC")],
    )
    assert is_excluded(cand, LISTS) == "institutional"


def test_months_between_never_negative() -> None:
    assert scoring.months_between(date(2026, 10, 1), date(2026, 9, 1)) == 0.0
    assert scoring.months_between(date(2026, 1, 1), date(2027, 1, 1)) == pytest.approx(
        12.0, abs=0.01
    )
