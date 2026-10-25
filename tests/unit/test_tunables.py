"""Tunables load from config/*.yaml and match REQUIREMENTS.md defaults."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from wholesaling.config.tunables import CONFIG_DIR, Tunables, get_tunables

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_config_dir_is_repo_config() -> None:
    assert CONFIG_DIR == REPO_ROOT / "config"
    for name in ("deal_band", "m1", "sequences", "a7", "caps"):
        assert (CONFIG_DIR / f"{name}.yaml").exists(), name


def test_v2_defaults_are_loaded() -> None:
    t = get_tunables()
    # M1-06 deal band
    assert t.deal_band.min_purchase == Decimal("75000")
    assert t.deal_band.max_purchase == Decimal("350000")
    # M1-02 weights
    w = t.m1.weights
    assert (w.demand, w.spread, w.distress, w.liquidity, w.price_fit, w.friction) == (
        0.30,
        0.25,
        0.20,
        0.10,
        0.10,
        -0.05,
    )
    assert t.m1.watch_to_active_min_buy_boxes == 10  # M1-04
    assert 3 <= t.m1.active_cap <= 5  # M1-04
    # OPN-01..05
    assert t.a7.assignment_fee == Decimal("10000")
    assert t.a7.market_pct == Decimal("0.70")
    assert t.a7.negotiation_room_pct == Decimal("0.07")
    assert t.sequences.investor_followup_days == [4, 4, 4]
    assert t.sequences.seller_followup_days == [3, 7, 14]
    # OPN-06
    assert t.caps.l1_daily_investor_sends == 50
    assert t.caps.l1_daily_seller_email_sends == 200
    # A7-03 comp tiers
    tiers = {c["tier"]: c for c in t.a7.comp_tiers}
    assert tiers[1] == {"tier": 1, "max_miles": 0.75, "max_days": 180}
    assert tiers[2] == {"tier": 2, "max_miles": 1.0, "max_days": 270}
    assert tiers[3] == {"tier": 3, "max_miles": 1.5, "max_days": 365}
    # A7-07, A7-08
    assert t.a7.offer_round_down_to == 500
    assert t.a7.min_offer_pct_of_arv == Decimal("0.35")
    assert t.a7.asking_tolerance_pct == Decimal("0.25")


def test_m1_weights_match_v2_formula_sum() -> None:
    # v2: 0.30 + 0.25 + 0.20 + 0.10 + 0.10 = 0.95 positive weight, friction -0.05 applied after.
    w = get_tunables().m1.weights
    assert abs(w.demand + w.spread + w.distress + w.liquidity + w.price_fit - 0.95) < 1e-9
    assert w.friction == -0.05


def test_missing_files_fall_back_to_model_defaults(tmp_path: Path) -> None:
    get_tunables.cache_clear()
    try:
        t = get_tunables(tmp_path)
        assert t == Tunables()
    finally:
        get_tunables.cache_clear()


def test_non_mapping_yaml_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "a7.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    get_tunables.cache_clear()
    try:
        with pytest.raises(ValueError, match="mapping"):
            get_tunables(tmp_path)
    finally:
        get_tunables.cache_clear()
