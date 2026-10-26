"""The A7 pipeline end to end on every fixture subject (A7-01..A7-18, ENG-Q11), with the numbers
worked by hand for the clean subject (see docs/agents/offers.md)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.agents.a7.conftest import (
    AVM_GAP,
    CLEAN,
    CONFLICT,
    DIRTY,
    DRIFT,
    SQFT_GAP,
    TIER2,
    UNKNOWN,
    MakeSubject,
)
from wholesaling.agents.a7 import mao as mao_mod
from wholesaling.agents.a7.engine import (
    ESCALATE_PHOTOS_NOTE,
    STATUS_BLOCKED,
    STATUS_ESCALATE,
    STATUS_READY,
    A7OfferEngine,
    OfferComputation,
)
from wholesaling.agents.a7.gates import (
    FLAG_HOA,
    GATE_ARV_CONFIDENCE,
    GATE_MIN_PCT_OF_ARV,
    GATE_NO_OPEN_FLAGS,
)
from wholesaling.db.models.enums import (
    ArvConfidence,
    ConditionClass,
    VerificationPass,
    VerificationResult,
)
from wholesaling.integrations.data.property import DEFAULT_TODAY, MockPropertyDataProvider, SoldComp


def _passes(comp: OfferComputation) -> list[tuple[str, int, str]]:
    return [(v.pass_id.value, v.iteration, v.result.value) for v in comp.verifications]


async def test_clean_subject_worked_example(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    """1204 Oak Hollow Dr: 1600 sqft, Medium + roof, asking $100,000.

    1 facts: county == provider (V1 pass)      2 comps: 7 at tier 1
    3 ARV: 150.1004 $/sqft x 1600 = 240,161; spread 9.99%; 7 comps tier 1 -> High
      V3: no flags (pass); V2: AVM 245,000 vs 240,161 = 2.0% gap (pass)
    4 repairs: Medium 30 x 1600 = 48,000 + roof 12,000 = 60,000 (V4 pass; remarks benign)
    5 MAO: 240,161 x 0.70 = 168,112.70 - 60,000 - 10,000 = 98,112.70; closing 2,943.38 -> 95,169.32
    6 offer: 95,169.32 x 0.93 = 88,507.47 -> $88,500 (V5 match)
    7 gates: all 7 pass (88,500 >= 84,056.35; 11.5% from asking; no flags)
    8 draft: locked template with $88,500 / 21 days
    """
    comp = await a7_engine.compute(make_subject(CLEAN))
    assert comp.status == STATUS_READY and comp.gates_passed
    assert comp.facts is not None and comp.facts.sqft == 1600 and comp.parcel_id == "HC-0001"
    assert comp.pull is not None and comp.pull.tier.tier == 1 and len(comp.comps) == 7
    assert comp.arv is not None and comp.arv.arv == Decimal(240161)
    assert comp.arv.weighted_price_per_sqft == Decimal("150.1004")
    assert comp.arv_confidence is ArvConfidence.HIGH and comp.avm == Decimal(245000)
    assert comp.condition_class is ConditionClass.MEDIUM
    assert comp.repairs is not None and comp.repairs.total == Decimal("60000.00")
    assert comp.mao is not None
    assert comp.mao.arv_times_pct == Decimal("168112.70")
    assert comp.mao.closing_costs == Decimal("2943.38")
    assert comp.mao.mao == Decimal("95169.32")
    assert comp.offer_price == Decimal(88500)
    assert _passes(comp) == [
        ("V1", 1, "pass"),
        ("V3", 1, "pass"),
        ("V2", 1, "pass"),
        ("V4", 1, "pass"),
        ("V5", 1, "pass"),
    ]
    assert [g.passed for g in comp.gates] == [True] * 7
    assert comp.draft is not None and "$88,500" in comp.draft.body and "21 days" in comp.draft.body
    assert comp.template_violations == [] and comp.blocked_reason is None
    record = comp.calc_record()
    assert record["sources_used"] == ["county", "provider"]
    assert record["gates_passed"] is True and record["status"] == "ready"
    assert len(record["comps_kept"]) == 7 and record["comps_rejected"] == []
    assert record["arv_avm"] == "245000" and record["arv_comps"]["arv"] == "240161"
    assert record["mao"]["inputs"]["offer_round_down_to"] == 500
    assert len(record["verifications"]) == 5


async def test_verification_order_follows_eng_q11(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(DIRTY))
    order = [v.pass_id for v in comp.verifications]
    assert order.index(VerificationPass.V1_DUAL_SOURCE_FACTS) == 0
    assert order.index(VerificationPass.V3_COMP_AUDIT) < order.index(
        VerificationPass.V2_DUAL_METHOD_ARV
    )
    assert order.index(VerificationPass.V2_DUAL_METHOD_ARV) < order.index(
        VerificationPass.V4_REPAIR_CROSS_CHECK
    )
    assert order[-1] is VerificationPass.V5_INDEPENDENT_RECOMPUTE
    assert VerificationPass.V6_PRE_SEND_DRIFT not in order  # only before send


async def test_tier2_subject_is_medium_confidence(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(
        make_subject(
            TIER2, condition_class=ConditionClass.LIGHT, disclosed_issues=[], asking_price=None
        )
    )
    assert comp.pull is not None and comp.pull.tier.tier == 2 and len(comp.comps) == 5
    assert comp.arv is not None and comp.arv.arv == Decimal(195208)
    assert comp.arv_confidence is ArvConfidence.MEDIUM  # tier 2 caps at Medium
    assert comp.repairs is not None and comp.repairs.total == Decimal(21000)
    assert (
        comp.mao is not None
        and comp.mao.mao == Decimal("102476.23")
        and comp.offer_price == Decimal(95000)
    )
    assert comp.status == STATUS_READY


async def test_sqft_disagreement_resolved_by_third_source(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(SQFT_GAP, disclosed_issues=[], asking_price=None))
    assert (
        comp.facts is not None
        and comp.facts.sqft == 1500
        and comp.facts.sources_used == ["county", "provider", "third"]
    )
    assert _passes(comp)[0] == ("V1", 1, "adjusted")
    assert comp.arv is not None and comp.arv.arv == Decimal(207491)  # 138.3274 x 1500
    assert comp.offer_price == Decimal(81000) and comp.status == STATUS_READY


async def test_three_way_conflict_forces_low_confidence_and_human(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(CONFLICT, disclosed_issues=[], asking_price=None))
    assert _passes(comp)[0] == ("V1", 1, "escalate")
    assert comp.escalations and comp.escalations[0].startswith("V1:")
    assert comp.arv_confidence is ArvConfidence.LOW  # forced by the escalation
    assert comp.status == STATUS_ESCALATE and not comp.gates_passed
    assert [g.name for g in comp.gates if not g.passed] == [GATE_ARV_CONFIDENCE]
    assert comp.offer_price == Decimal(74500)  # the number is still computed for the human
    assert "forced to Low" in " ".join(comp.confidence_notes)


async def test_dirty_comps_are_audited_out_and_v4_bumps_class(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(DIRTY, disclosed_issues=[], asking_price=None))
    rejected = {r["comp"]["comp_id"]: r["flags"] for r in comp.comps_rejected}
    assert rejected == {
        "DC-NAL": ["non_arms_length"],
        "DC-NEW": ["new_build"],
        "DC-FLIP": ["renovated_flip"],
        "DC-LOT": ["lot_value_outlier"],
    }
    assert [c.comp_id for c in comp.comps] == ["DC1", "DC4", "DC2", "DC3", "DC5"]
    assert ("V3", 1, "adjusted") in _passes(comp) and ("V3", 2, "pass") in _passes(comp)
    assert (
        comp.arv is not None
        and comp.arv.arv == Decimal(203133)
        and comp.arv_confidence is ArvConfidence.HIGH
    )
    # V4: remarks "as-is ... foundation ... investor special" -> Heavy evidence; Medium -> Heavy
    assert comp.condition_class is ConditionClass.HEAVY
    v4 = next(v for v in comp.verifications if v.pass_id is VerificationPass.V4_REPAIR_CROSS_CHECK)
    assert v4.result is VerificationResult.ADJUSTED and v4.outputs["foundation_inferred"] is True
    assert comp.repairs is not None
    assert comp.repairs.base == Decimal(75000) and comp.repairs.foundation_contingency == Decimal(
        12000
    )
    assert comp.repairs.total == Decimal(87000)
    # 203,133 x 0.7 = 142,193.10 - 87,000 - 10,000 = 45,193.10; closing 1,355.79; MAO 43,837.31
    assert (
        comp.mao is not None
        and comp.mao.mao == Decimal("43837.31")
        and comp.offer_price == Decimal(40500)
    )
    # 40,500 < 35% of 203,133 (71,096.55): the min-pct gate fails -> human.
    assert [g.name for g in comp.gates if not g.passed] == [GATE_MIN_PCT_OF_ARV]
    assert comp.status == STATUS_ESCALATE


async def test_avm_gap_reruns_comps_at_tier_2_then_passes(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(
        make_subject(
            AVM_GAP, condition_class=ConditionClass.LIGHT, disclosed_issues=[], asking_price=None
        )
    )
    v2 = [v for v in comp.verifications if v.pass_id is VerificationPass.V2_DUAL_METHOD_ARV]
    assert [(v.iteration, v.result.value) for v in v2] == [(1, "adjusted"), (2, "adjusted")]
    assert Decimal(v2[0].outputs["gap_pct"]) > Decimal("0.10")
    assert Decimal(v2[1].outputs["gap_pct"]) <= Decimal("0.10")
    assert comp.pull is not None and comp.pull.tier.tier == 2 and len(comp.comps) == 9
    assert comp.arv is not None and comp.arv.arv == Decimal(283572)
    assert comp.arv_confidence is ArvConfidence.MEDIUM and comp.status == STATUS_READY
    assert comp.offer_price == Decimal(149500)


async def test_unknown_address_is_blocked_with_only_v1(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(UNKNOWN))
    assert comp.status == STATUS_BLOCKED and comp.blocked_reason is not None
    assert "could not be resolved" in comp.blocked_reason
    assert _passes(comp) == [("V1", 1, "escalate")] and comp.offer_price is None
    assert comp.calc_record()["status"] == "blocked"


async def test_open_flags_fail_the_gate_list(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(CLEAN, hoa="Oak Hollow HOA", hoa_unknown=False))
    assert comp.open_flags == [FLAG_HOA]
    assert [g.name for g in comp.gates if not g.passed] == [GATE_NO_OPEN_FLAGS]
    assert comp.status == STATUS_ESCALATE and comp.offer_price == Decimal(88500)


async def test_suppressed_seller_and_active_parcel_offer_fail_gates(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(
        make_subject(CLEAN, seller_suppressed=True, active_offer_parcel_ids=frozenset({"HC-0001"}))
    )
    assert [g.name for g in comp.gates if not g.passed] == [
        "seller_not_suppressed",
        "no_active_offer_on_parcel",
    ]


async def test_no_photos_drops_confidence_one_level(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(CLEAN, photo_count=0))
    assert (
        comp.arv_confidence is ArvConfidence.MEDIUM
        and ESCALATE_PHOTOS_NOTE in comp.confidence_notes
    )
    assert comp.status == STATUS_READY  # Medium still passes gate 1


async def test_market_hard_cap_override(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(CLEAN, market_hard_cap=Decimal(80000)))
    assert comp.hard_cap == Decimal(80000)
    assert [g.name for g in comp.gates if not g.passed] == ["market_hard_cap"]


async def test_v5_mismatch_blocks_the_offer(
    a7_engine: A7OfferEngine, make_subject: MakeSubject, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wholesaling.agents.a7.engine as engine_mod

    real = mao_mod.compute_mao

    def buggy(inputs: mao_mod.MaoInputs) -> mao_mod.MaoResult:
        res = real(inputs)
        return mao_mod.MaoResult(
            inputs=res.inputs,
            arv_times_pct=res.arv_times_pct,
            pre_closing=res.pre_closing,
            closing_costs=res.closing_costs,
            mao=res.mao,
            offer_raw=res.offer_raw,
            offer=res.offer + 500,
        )

    monkeypatch.setattr(engine_mod, "compute_mao", buggy)
    comp = await a7_engine.compute(make_subject(CLEAN))
    assert comp.status == STATUS_BLOCKED and comp.blocked_reason is not None
    assert comp.blocked_reason.startswith("V5 recompute mismatch")
    assert _passes(comp)[-1] == ("V5", 1, "fail")
    assert comp.gates == [] and comp.draft is None  # the pipeline stops before step 7


# -- V6 ------------------------------------------------------------------------------------------


def _inject(provider: MockPropertyDataProvider, cid: str, ppsf: int) -> None:
    provider.inject_comp(
        DRIFT,
        SoldComp(
            cid,
            f"P-{cid}",
            f"{cid} New St",
            "77009",
            Decimal(ppsf * 1500),
            DEFAULT_TODAY,
            1500,
            Decimal("0.10"),
            year_built=1980,
        ),
    )


async def test_v6_pass_when_nothing_changed(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(DRIFT, disclosed_issues=[], asking_price=None))
    assert comp.offer_price == Decimal(92500)
    comp = await a7_engine.pre_send_check(comp)
    v6 = comp.verifications[-1]
    assert v6.pass_id is VerificationPass.V6_PRE_SEND_DRIFT and v6.result is VerificationResult.PASS
    assert v6.outputs["gap_pct"] == "0.0000" and v6.outputs["gates_passed"] is True
    assert (
        comp.offer_price == Decimal(92500)
        and comp.next_iteration(VerificationPass.V6_PRE_SEND_DRIFT) == 2
    )


async def test_v6_three_new_comps_move_arv_over_five_percent_and_recompute(
    a7_engine: A7OfferEngine, make_subject: MakeSubject, provider: MockPropertyDataProvider
) -> None:
    comp = await a7_engine.compute(make_subject(DRIFT, disclosed_issues=[], asking_price=None))
    arv_before, offer_before = comp.arv.arv, comp.offer_price  # type: ignore[union-attr]
    # Three recent, close sales at $170/sqft (8 comps: one 170 and the 148 are trimmed).
    _inject(provider, "NEW1", 170)
    _inject(provider, "NEW2", 170)
    _inject(provider, "NEW3", 170)
    comp = await a7_engine.pre_send_check(comp)
    v6 = comp.verifications[-1]
    assert v6.result is VerificationResult.ADJUSTED
    assert v6.inputs["new_comp_ids"] == ["NEW1", "NEW2", "NEW3"]
    assert Decimal(v6.outputs["gap_pct"]) > Decimal("0.05")
    assert comp.arv is not None and comp.arv.arv > arv_before and len(comp.comps) == 8
    assert (
        comp.offer_price is not None
        and offer_before is not None
        and comp.offer_price > offer_before
    )
    assert comp.arv_confidence is ArvConfidence.MEDIUM  # spread now > 12%
    assert comp.gates_passed and comp.status == STATUS_READY
    # V5 re-ran as iteration 2 and the draft carries the new number.
    assert ("V5", 2, "pass") in _passes(comp)
    assert comp.draft is not None and f"${int(comp.offer_price):,}" in comp.draft.body


async def test_v6_outlier_comp_breaks_confidence_and_fails_gates(
    a7_engine: A7OfferEngine, make_subject: MakeSubject, provider: MockPropertyDataProvider
) -> None:
    comp = await a7_engine.compute(make_subject(DRIFT, disclosed_issues=[], asking_price=None))
    _inject(provider, "HOT", 200)
    comp = await a7_engine.pre_send_check(comp)
    assert comp.verifications[-1].result is VerificationResult.ADJUSTED
    assert comp.arv is not None and comp.arv.arv == Decimal(244533)
    assert comp.arv_confidence is ArvConfidence.LOW and comp.status == STATUS_ESCALATE
    assert [g.name for g in comp.gates if not g.passed] == [GATE_ARV_CONFIDENCE]


async def test_v6_prefilters_a_new_dirty_comp(
    a7_engine: A7OfferEngine, make_subject: MakeSubject, provider: MockPropertyDataProvider
) -> None:
    comp = await a7_engine.compute(make_subject(DRIFT, disclosed_issues=[], asking_price=None))
    provider.inject_comp(
        DRIFT,
        SoldComp(
            "LAND",
            "P-L",
            "1 Land",
            "77009",
            Decimal(600 * 1500),
            DEFAULT_TODAY,
            1500,
            Decimal("0.1"),
            year_built=1980,
            remarks="value in land",
        ),
    )
    comp = await a7_engine.pre_send_check(comp)
    v6 = comp.verifications[-1]
    assert v6.result is VerificationResult.PASS
    assert v6.inputs["auto_rejected"] == [{"comp_id": "LAND", "flags": ["lot_value_outlier"]}]
    assert comp.comps_rejected[-1]["stage"] == "V6"


async def test_v6_skips_blocked_computations(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    comp = await a7_engine.compute(make_subject(UNKNOWN))
    before = len(comp.verifications)
    comp = await a7_engine.pre_send_check(comp)
    assert len(comp.verifications) == before


# -- restore -------------------------------------------------------------------------------------


async def test_restore_rehydrates_a_computation_from_its_calc_record(
    a7_engine: A7OfferEngine, make_subject: MakeSubject
) -> None:
    subject = make_subject(CLEAN)
    comp = await a7_engine.compute(subject)
    record = comp.calc_record()
    restored = OfferComputation.restore(subject, record)
    assert restored.arv is not None and restored.arv.arv == comp.arv.arv  # type: ignore[union-attr]
    assert restored.mao is not None and restored.mao.offer == comp.offer_price
    assert restored.arv_confidence is comp.arv_confidence and restored.status == comp.status
    assert [v.to_json() for v in restored.verifications] == [
        v.to_json() for v in comp.verifications
    ]
    assert restored.pull is not None and restored.pull.tier.tier == 1 and len(restored.comps) == 7
    assert restored.repairs == comp.repairs and restored.draft == comp.draft
    assert restored.calc_record() == record
    # And V6 runs on the restored object exactly as on the original.
    again = await a7_engine.pre_send_check(restored)
    assert again.verifications[-1].pass_id is VerificationPass.V6_PRE_SEND_DRIFT
