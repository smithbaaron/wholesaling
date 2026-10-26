"""Step 4 repairs (A7-05, OPN-12): rate table x sqft + line items + Houston foundation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wholesaling.agents.a7.repairs import (
    LINE_ITEM_DEFAULTS,
    RepairEstimate,
    bump_class,
    estimate_repairs,
    is_houston,
    normalize_issue,
    rate_for,
)
from wholesaling.config.tunables import A7Config
from wholesaling.db.models.enums import ConditionClass


@pytest.mark.parametrize(
    ("cls", "rate"),
    [
        (ConditionClass.LIGHT, "15"),
        (ConditionClass.MEDIUM, "30"),
        (ConditionClass.HEAVY, "50"),
        (ConditionClass.GUT, "75"),
    ],
)
def test_rate_table_from_config(a7_config: A7Config, cls: ConditionClass, rate: str) -> None:
    assert rate_for(a7_config, cls) == Decimal(rate)


def test_rate_for_missing_class_fails_fast(a7_config: A7Config) -> None:
    bad = a7_config.model_copy(update={"repair_rate_per_sqft": {"light": Decimal(15)}})
    with pytest.raises(KeyError, match="no rate"):
        rate_for(bad, ConditionClass.GUT)


def test_base_only(a7_config: A7Config) -> None:
    est = estimate_repairs(
        a7_config,
        sqft=1600,
        condition_class=ConditionClass.MEDIUM,
        disclosed_issues=[],
        market_name="Dallas",
    )
    assert est.base == Decimal("48000.00") and est.total == Decimal("48000.00")
    assert est.line_items == {} and est.foundation_contingency == 0 and not est.houston


def test_line_items_added_and_normalised(a7_config: A7Config) -> None:
    est = estimate_repairs(
        a7_config,
        sqft=1600,
        condition_class=ConditionClass.MEDIUM,
        disclosed_issues=["Roof leak", "A/C is dead", "peeling paint"],
        market_name="Dallas",
    )
    assert est.line_items == {"hvac": Decimal(8000), "roof": Decimal(12000)}
    assert est.unpriced_issues == ["peeling paint"]
    assert est.total == Decimal(48000) + Decimal(20000)


def test_houston_foundation_uses_fixed_contingency(a7_config: A7Config) -> None:
    est = estimate_repairs(
        a7_config,
        sqft=1500,
        condition_class=ConditionClass.HEAVY,
        disclosed_issues=["foundation cracks"],
        market_name="Houston",
    )
    assert est.houston and est.foundation_contingency == Decimal(12000)
    assert "foundation" not in est.line_items
    assert est.total == Decimal(75000) + Decimal(12000)


def test_houston_foundation_inferred_by_v4_also_adds_contingency(a7_config: A7Config) -> None:
    est = estimate_repairs(
        a7_config,
        sqft=1500,
        condition_class=ConditionClass.MEDIUM,
        disclosed_issues=[],
        market_name="Houston, TX",
        foundation_inferred=True,
    )
    assert est.foundation_contingency == Decimal(12000) and est.total == Decimal(57000)


def test_foundation_outside_houston_is_a_generic_line_item(a7_config: A7Config) -> None:
    est = estimate_repairs(
        a7_config,
        sqft=1500,
        condition_class=ConditionClass.MEDIUM,
        disclosed_issues=["foundation"],
        market_name="Dallas",
        city="Dallas",
    )
    assert not est.houston and est.foundation_contingency == 0
    assert est.line_items == {"foundation": LINE_ITEM_DEFAULTS["foundation"]}


def test_city_alone_identifies_houston(a7_config: A7Config) -> None:
    assert is_houston(None, "Houston")
    assert is_houston("houston, tx")
    assert not is_houston("Dallas", "Plano")
    est = estimate_repairs(
        a7_config,
        sqft=1000,
        condition_class=ConditionClass.LIGHT,
        disclosed_issues=["slab"],
        market_name="",
        city="Houston",
    )
    assert est.foundation_contingency == Decimal(12000)


def test_rate_override_for_market_table(a7_config: A7Config) -> None:
    est = estimate_repairs(
        a7_config,
        sqft=1000,
        condition_class=ConditionClass.LIGHT,
        disclosed_issues=[],
        market_name="Dallas",
        rate_override=Decimal("18"),
    )
    assert est.rate_per_sqft == Decimal(18) and est.base == Decimal(18000)


def test_bump_class_one_level_and_gut_is_terminal() -> None:
    assert bump_class(ConditionClass.LIGHT) is ConditionClass.MEDIUM
    assert bump_class(ConditionClass.MEDIUM) is ConditionClass.HEAVY
    assert bump_class(ConditionClass.HEAVY) is ConditionClass.GUT
    assert bump_class(ConditionClass.GUT) is ConditionClass.GUT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("roof", "roof"),
        ("Roof needs replacing", "roof"),
        ("HVAC", "hvac"),
        ("furnace out", "hvac"),
        ("foundation issues", "foundation"),
        ("piers", "foundation"),
        ("windows", None),
        ("", None),
    ],
)
def test_normalize_issue(raw: str, expected: str | None) -> None:
    assert normalize_issue(raw) == expected


def test_sqft_must_be_positive(a7_config: A7Config) -> None:
    with pytest.raises(ValueError, match="positive"):
        estimate_repairs(
            a7_config,
            sqft=0,
            condition_class=ConditionClass.LIGHT,
            disclosed_issues=[],
            market_name="x",
        )


def test_json_round_trip(a7_config: A7Config) -> None:
    est = estimate_repairs(
        a7_config,
        sqft=1500,
        condition_class=ConditionClass.HEAVY,
        disclosed_issues=["roof", "foundation", "well pump"],
        market_name="Houston",
    )
    doc = est.to_json()
    assert doc["total"] == "99000.00" and doc["foundation_contingency"] == "12000"
    assert RepairEstimate.from_json(doc) == est
