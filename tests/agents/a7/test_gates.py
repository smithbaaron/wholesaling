"""Step 7 gates (A7-08, OPN-16): every gate individually, plus the open-flag derivation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wholesaling.agents.a7.gates import (
    FLAG_CO_OWNER_MISSING,
    FLAG_HOA,
    FLAG_MULTI_UNIT,
    FLAG_NON_RESIDENTIAL,
    FLAG_PROBATE_WITHOUT_AUTHORITY,
    FLAG_TITLE_LIEN,
    GATE_ARV_CONFIDENCE,
    GATE_ASKING_TOLERANCE,
    GATE_MARKET_HARD_CAP,
    GATE_MIN_PCT_OF_ARV,
    GATE_NAMES,
    GATE_NO_ACTIVE_OFFER,
    GATE_NO_OPEN_FLAGS,
    GATE_SELLER_NOT_SUPPRESSED,
    FlagInputs,
    GateInputs,
    all_passed,
    derive_open_flags,
    evaluate_gates,
    failed_names,
)
from wholesaling.config.tunables import A7Config
from wholesaling.db.models.enums import ArvConfidence


def _inputs(**overrides: object) -> GateInputs:
    base: dict[str, object] = {
        "arv": Decimal(240161),
        "arv_confidence": ArvConfidence.HIGH,
        "offer": Decimal(88500),
        "hard_cap": Decimal(350000),
        "asking_price": Decimal(100000),
        "open_flags": [],
        "seller_suppressed": False,
        "active_offer_on_parcel": False,
    }
    base.update(overrides)
    return GateInputs(**base)  # type: ignore[arg-type]


def _by_name(a7_config: A7Config, inp: GateInputs) -> dict[str, bool]:
    return {g.name: g.passed for g in evaluate_gates(a7_config, inp)}


def test_all_seven_gates_in_order_and_all_pass_on_clean_inputs(a7_config: A7Config) -> None:
    gates = evaluate_gates(a7_config, _inputs())
    assert [g.name for g in gates] == list(GATE_NAMES) and len(gates) == 7
    assert all_passed(gates) and failed_names(gates) == []
    assert all_passed([]) is False


@pytest.mark.parametrize(
    ("confidence", "ok"),
    [(ArvConfidence.HIGH, True), (ArvConfidence.MEDIUM, True), (ArvConfidence.LOW, False)],
)
def test_gate_arv_confidence(a7_config: A7Config, confidence: ArvConfidence, ok: bool) -> None:
    assert _by_name(a7_config, _inputs(arv_confidence=confidence))[GATE_ARV_CONFIDENCE] is ok


def test_gate_market_hard_cap(a7_config: A7Config) -> None:
    assert _by_name(a7_config, _inputs(offer=Decimal(350000)))[GATE_MARKET_HARD_CAP]
    assert not _by_name(a7_config, _inputs(offer=Decimal(350500)))[GATE_MARKET_HARD_CAP]
    assert not _by_name(a7_config, _inputs(hard_cap=Decimal(80000)))[GATE_MARKET_HARD_CAP]


def test_gate_min_pct_of_arv(a7_config: A7Config) -> None:
    # 35% of 240,161 = 84,056.35
    assert _by_name(a7_config, _inputs(offer=Decimal("84056.35")))[GATE_MIN_PCT_OF_ARV]
    assert not _by_name(a7_config, _inputs(offer=Decimal(84000)))[GATE_MIN_PCT_OF_ARV]
    assert not _by_name(a7_config, _inputs(offer=Decimal(0), arv=Decimal(0)))[GATE_MIN_PCT_OF_ARV]
    gate = next(g for g in evaluate_gates(a7_config, _inputs()) if g.name == GATE_MIN_PCT_OF_ARV)
    assert gate.threshold == Decimal("84056.35")


def test_gate_asking_tolerance(a7_config: A7Config) -> None:
    # 88,500 vs 100,000 -> 11.5% deviation: pass
    assert _by_name(a7_config, _inputs())[GATE_ASKING_TOLERANCE]
    assert _by_name(a7_config, _inputs(asking_price=Decimal(118000)))[GATE_ASKING_TOLERANCE]
    assert not _by_name(a7_config, _inputs(asking_price=Decimal(120000)))[GATE_ASKING_TOLERANCE]
    assert _by_name(a7_config, _inputs(asking_price=None))[GATE_ASKING_TOLERANCE]
    assert _by_name(a7_config, _inputs(asking_price=Decimal(0)))[GATE_ASKING_TOLERANCE]
    gate = next(
        g
        for g in evaluate_gates(a7_config, _inputs(asking_price=None))
        if g.name == GATE_ASKING_TOLERANCE
    )
    assert gate.detail == "asking price blank" and gate.value is None


def test_gate_no_open_flags(a7_config: A7Config) -> None:
    assert not _by_name(a7_config, _inputs(open_flags=[FLAG_HOA]))[GATE_NO_OPEN_FLAGS]
    gate = next(
        g
        for g in evaluate_gates(a7_config, _inputs(open_flags=[FLAG_HOA, FLAG_TITLE_LIEN]))
        if g.name == GATE_NO_OPEN_FLAGS
    )
    assert gate.value == [FLAG_HOA, FLAG_TITLE_LIEN]


def test_gate_seller_not_suppressed(a7_config: A7Config) -> None:
    assert not _by_name(a7_config, _inputs(seller_suppressed=True))[GATE_SELLER_NOT_SUPPRESSED]


def test_gate_no_active_offer_on_parcel(a7_config: A7Config) -> None:
    assert not _by_name(a7_config, _inputs(active_offer_on_parcel=True))[GATE_NO_ACTIVE_OFFER]


def test_gate_results_serialise(a7_config: A7Config) -> None:
    doc = [
        g.to_json() for g in evaluate_gates(a7_config, _inputs(arv_confidence=ArvConfidence.LOW))
    ]
    assert doc[0] == {
        "name": GATE_ARV_CONFIDENCE,
        "passed": False,
        "value": "Low",
        "threshold": ">= Medium",
        "detail": "ARV confidence must be High or Medium; Low goes to a human (A7-04)",
    }
    assert doc[1]["value"] == "88500" and doc[1]["threshold"] == "350000"


# -- open flags ----------------------------------------------------------------------------------


def test_no_flags_when_everything_answered_negatively() -> None:
    flags = derive_open_flags(
        FlagInputs(
            liens="none",
            liens_unknown=False,
            hoa="No",
            hoa_unknown=False,
            co_owners="none",
            co_owners_unknown=False,
            motivation="downsizing",
            property_type="sfr",
            units=1,
        )
    )
    assert flags == []


def test_stated_lien_and_hoa_flag_but_unknown_does_not() -> None:
    assert derive_open_flags(
        FlagInputs(liens="tax lien 2024", liens_unknown=False, co_owners_unknown=False)
    ) == [FLAG_TITLE_LIEN]
    assert derive_open_flags(
        FlagInputs(hoa="Oak HOA $40/mo", hoa_unknown=False, co_owners_unknown=False)
    ) == [FLAG_HOA]
    assert (
        derive_open_flags(FlagInputs(liens_unknown=True, hoa_unknown=True, co_owners_unknown=False))
        == []
    )


def test_multi_unit_and_non_residential_from_facts() -> None:
    assert FLAG_MULTI_UNIT in derive_open_flags(
        FlagInputs(co_owners_unknown=False, property_type="duplex", units=2)
    )
    assert FLAG_NON_RESIDENTIAL in derive_open_flags(
        FlagInputs(co_owners_unknown=False, property_type="commercial", units=1)
    )
    assert (
        derive_open_flags(FlagInputs(co_owners_unknown=False, property_type="condo", units=1)) == []
    )


def test_probate_without_authority() -> None:
    assert derive_open_flags(
        FlagInputs(co_owners_unknown=False, motivation="inherited the house, in probate")
    ) == [FLAG_PROBATE_WITHOUT_AUTHORITY]
    assert (
        derive_open_flags(
            FlagInputs(
                co_owners_unknown=False,
                motivation="probate; I am the executor with letters testamentary",
            )
        )
        == []
    )


def test_co_owner_missing() -> None:
    assert derive_open_flags(FlagInputs(co_owners_unknown=True)) == [FLAG_CO_OWNER_MISSING]
    assert derive_open_flags(FlagInputs(co_owners="my brother", co_owners_unknown=False)) == [
        FLAG_CO_OWNER_MISSING
    ]
    assert (
        derive_open_flags(
            FlagInputs(co_owners="brother, identified and on the deed", co_owners_unknown=False)
        )
        == []
    )
