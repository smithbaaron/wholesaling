"""V1, V2, V3, V4 rules in isolation (A7-12..A7-15): outcomes, loops and the iteration caps."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.agents.a7.conftest import CLEAN, CONFLICT, SQFT_GAP, UNKNOWN
from wholesaling.agents.a7.arv import ArvEstimate, compute_arv
from wholesaling.agents.a7.audit import CompAuditResult, CompVerdict
from wholesaling.agents.a7.comps import CompPull, CompTier
from wholesaling.agents.a7.verification import (
    V2_MAX_PASSES,
    V3_MAX_ITERATIONS,
    PassRecord,
    ResolvedFacts,
    drift_exceeds,
    evidence_condition,
    facts_agree,
    run_v1,
    run_v2,
    run_v3,
    run_v4,
)
from wholesaling.db.models.enums import (
    ArvConfidence,
    ConditionClass,
    VerificationPass,
    VerificationResult,
)
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    ListingInfo,
    MockPropertyDataProvider,
    ParcelFacts,
    PropertyAddress,
    SoldComp,
)

TIER1 = CompTier(1, Decimal("0.75"), 180)
TIER2 = CompTier(2, Decimal("1.0"), 270)
TIER3 = CompTier(3, Decimal("1.5"), 365)


def _comp(cid: str, ppsf: int, days: int = 30, miles: str = "0.3") -> SoldComp:
    return SoldComp(
        comp_id=cid,
        parcel_id=None,
        address=cid,
        postal_code="77008",
        sale_price=Decimal(ppsf * 1500),
        sale_date=DEFAULT_TODAY - timedelta(days=days),
        sqft=1500,
        distance_miles=Decimal(miles),
        year_built=1990,
    )


def _arv(comps: list[SoldComp], tier: CompTier = TIER1) -> ArvEstimate | None:
    return compute_arv(comps, subject_sqft=1500, tier=tier, as_of=DEFAULT_TODAY) if comps else None


# -- V1 ------------------------------------------------------------------------------------------


def test_facts_agree_within_ten_percent() -> None:
    assert facts_agree(1500, 1650)  # 9.1%
    assert not facts_agree(1500, 1700)  # 11.8%
    assert not facts_agree(None, 1500)
    assert facts_agree(Decimal("2.0"), Decimal("2.0"))


async def test_v1_pass_when_sources_agree(provider: MockPropertyDataProvider) -> None:
    facts, record = await run_v1(provider, CLEAN)
    assert facts is not None and facts.sqft == 1600 and facts.parcel_id == "HC-0001"
    assert record.pass_id is VerificationPass.V1_DUAL_SOURCE_FACTS and record.iteration == 1
    assert record.result is VerificationResult.PASS
    assert facts.sources_used == ["county", "provider"] and facts.unresolved == []
    assert [c["method"] for c in provider.calls] == [
        "county_facts",
        "provider_facts",
    ]  # no third pull


async def test_v1_pulls_third_source_and_majority_wins(provider: MockPropertyDataProvider) -> None:
    facts, record = await run_v1(provider, SQFT_GAP)
    assert facts is not None and facts.sqft == 1500  # county 1500 + third 1520 agree
    assert record.result is VerificationResult.ADJUSTED
    assert record.outputs["disagreeing"] == ["sqft"]
    assert facts.field_sources["sqft"] == "county+third"
    assert facts.field_sources["beds"] == "county"
    assert [c["method"] for c in provider.calls][-1] == "third_facts"
    assert record.inputs["third"]["sqft"] == 1520


async def test_v1_escalates_without_majority(provider: MockPropertyDataProvider) -> None:
    facts, record = await run_v1(provider, CONFLICT)
    assert record.result is VerificationResult.ESCALATE and "sqft" in record.reason
    assert facts is not None and facts.unresolved == ["sqft"] and facts.sqft == 1500  # provisional


async def test_v1_escalates_when_no_source_knows_the_address(
    provider: MockPropertyDataProvider,
) -> None:
    facts, record = await run_v1(provider, UNKNOWN)
    assert facts is None and record.result is VerificationResult.ESCALATE
    assert "could not be resolved" in record.reason


async def test_v1_single_source_is_escalated_but_usable(provider: MockPropertyDataProvider) -> None:
    only_third = PropertyAddress("5 Third Only", "Houston", "TX", "77008")
    provider.subjects[only_third.key] = type(provider.subjects[CLEAN.key])(
        key=only_third.key,
        county=None,
        provider=None,
        third=ParcelFacts("third", "T-1", sqft=1200, year_built=1990),
        avm=None,
        listing=None,
        comps=[],
    )
    facts, record = await run_v1(provider, only_third)
    assert facts is not None and facts.sqft == 1200 and record.result is VerificationResult.ESCALATE
    assert "only one source" in record.reason


async def test_v1_provider_parcel_mismatch_escalates(provider: MockPropertyDataProvider) -> None:
    subject = provider.subjects[CLEAN.key]
    subject.provider = ParcelFacts.from_json({**subject.provider.to_json(), "parcel_id": "OTHER"})  # type: ignore[union-attr]
    facts, record = await run_v1(provider, CLEAN)
    assert record.result is VerificationResult.ESCALATE
    assert facts is not None and "parcel_id" in facts.unresolved


def test_resolved_facts_round_trip() -> None:
    facts = ResolvedFacts(
        "P", 1500, 3, Decimal("2.5"), 1990, 7000, "sfr", 1, date(2015, 1, 2), Decimal(100), "O"
    )
    assert ResolvedFacts.from_json(facts.to_json()) == facts


# -- V3 ------------------------------------------------------------------------------------------


class ScriptedAuditor:
    """Returns the scripted flags per iteration; records what it was asked to audit."""

    def __init__(self, rounds: list[dict[str, list[str]]]) -> None:
        self.rounds = rounds
        self.seen: list[list[str]] = []

    async def __call__(self, comps: Sequence[SoldComp]) -> CompAuditResult:
        self.seen.append([c.comp_id for c in comps])
        flags = self.rounds[len(self.seen) - 1] if len(self.seen) <= len(self.rounds) else {}
        verdicts = {c.comp_id: CompVerdict(c.comp_id) for c in comps}
        for cid, fl in flags.items():
            if cid in verdicts:
                for f in fl:
                    verdicts[cid].add(f, f"scripted {f}", "model")
        return CompAuditResult(verdicts=verdicts, run_id=None, model="mock", rationale="scripted")


async def test_v3_no_flags_is_one_pass_record() -> None:
    comps = [_comp("a", 150), _comp("b", 152), _comp("c", 148)]
    out = await run_v3(ScriptedAuditor([{}]), comps, _arv(comps), _arv)
    assert out.iterations == 1 and [r.result for r in out.records] == [VerificationResult.PASS]
    assert out.comps == comps and out.rejected == [] and out.arv == _arv(comps)


async def test_v3_removes_flagged_recomputes_then_stops_when_clean() -> None:
    comps = [_comp("a", 150), _comp("b", 152), _comp("c", 148), _comp("flip", 200)]
    auditor = ScriptedAuditor([{"flip": ["renovated_flip"]}, {}])
    out = await run_v3(auditor, comps, _arv(comps), _arv)
    assert auditor.seen == [["a", "b", "c", "flip"], ["a", "b", "c"]]
    assert [(r.iteration, r.result) for r in out.records] == [
        (1, VerificationResult.ADJUSTED),
        (2, VerificationResult.PASS),
    ]
    assert [r["comp"]["comp_id"] for r in out.rejected] == ["flip"]
    assert out.rejected[0]["flags"] == ["renovated_flip"]
    assert out.arv is not None and out.arv.comp_count == 3
    assert out.records[0].outputs["removed"] == ["flip"]
    assert Decimal(out.records[0].inputs["arv_before"]["arv"]) > out.arv.arv


async def test_v3_stops_at_two_iterations_even_with_new_flags_each_time() -> None:
    comps = [_comp(c, 150) for c in ("a", "b", "c", "d", "e")]
    auditor = ScriptedAuditor([{"a": ["new_build"]}, {"b": ["new_build"]}, {"c": ["new_build"]}])
    out = await run_v3(auditor, comps, _arv(comps), _arv)
    assert out.iterations == V3_MAX_ITERATIONS == 2
    assert len(auditor.seen) == 2
    assert [c.comp_id for c in out.comps] == ["c", "d", "e"]  # third round never ran
    assert all(r.result is VerificationResult.ADJUSTED for r in out.records)


async def test_v3_escalates_when_every_comp_is_flagged() -> None:
    comps = [_comp("a", 150), _comp("b", 150)]
    out = await run_v3(
        ScriptedAuditor([{"a": ["new_build"], "b": ["lot_value_outlier"]}]),
        comps,
        _arv(comps),
        _arv,
    )
    assert out.comps == [] and out.arv is None
    assert out.records[-1].result is VerificationResult.ESCALATE and len(out.records) == 1


# -- V2 ------------------------------------------------------------------------------------------


def _pull(tier: CompTier, comps: list[SoldComp]) -> CompPull:
    return CompPull(tier=tier, comps=comps)


class TierPuller:
    def __init__(self, by_tier: dict[int, list[SoldComp]]) -> None:
        self.by_tier = by_tier
        self.asked: list[int] = []

    async def __call__(self, tier: int) -> CompPull:
        self.asked.append(tier)
        t = {1: TIER1, 2: TIER2, 3: TIER3}[tier]
        return _pull(t, self.by_tier.get(tier, []))


async def test_v2_pass_within_ten_percent() -> None:
    comps = [_comp(c, 150) for c in "abcde"]
    arv = _arv(comps)
    assert arv is not None
    out = await run_v2(
        arv=arv,
        avm=Decimal(240000),
        pull=_pull(TIER1, comps),
        max_tier=3,
        pull_at_tier=TierPuller({}),
        arv_for=lambda p: _arv(list(p.comps), p.tier),
    )
    assert [r.result for r in out.records] == [VerificationResult.PASS]
    assert out.confidence is ArvConfidence.HIGH and not out.penalised
    assert out.records[0].outputs["gap_pct"] == "0.0625"  # 225,000 vs 240,000


async def test_v2_expands_tier_and_passes_on_second_compare() -> None:
    t1 = [_comp(c, 200) for c in "abcde"]
    t2 = [*t1, *(_comp(c, 170, days=220, miles="0.9") for c in "wxyz")]
    puller = TierPuller({2: t2})
    out = await run_v2(
        arv=_arv(t1),
        avm=Decimal(262500),
        pull=_pull(TIER1, t1),
        max_tier=3,
        pull_at_tier=puller,
        arv_for=lambda p: _arv(list(p.comps), p.tier),
    )
    assert puller.asked == [2]
    assert [(r.iteration, r.result) for r in out.records] == [
        (1, VerificationResult.ADJUSTED),
        (2, VerificationResult.ADJUSTED),
    ]
    assert out.pull.tier.tier == 2 and out.arv is not None and out.arv.tier == 2
    assert not out.penalised and out.confidence is out.arv.confidence
    assert "re-running comps at tier 2" in out.records[0].reason


async def test_v2_three_passes_then_confidence_minus_one() -> None:
    comps = [_comp(c, 200) for c in "abcde"]
    puller = TierPuller({2: comps, 3: comps})  # the gap never closes
    out = await run_v2(
        arv=_arv(comps),
        avm=Decimal(200000),
        pull=_pull(TIER1, comps),
        max_tier=3,
        pull_at_tier=puller,
        arv_for=lambda p: _arv(list(p.comps), p.tier),
    )
    assert puller.asked == [2, 3]
    assert len(out.records) == V2_MAX_PASSES == 3
    assert [r.iteration for r in out.records] == [1, 2, 3]
    assert out.records[-1].outputs["confidence_before"] == "Low"  # tier 3 -> Low by the rules
    assert out.penalised and out.confidence is ArvConfidence.LOW
    assert "confidence lowered one level" in out.records[-1].reason


async def test_v2_penalises_when_no_higher_tier_exists() -> None:
    comps = [_comp(c, 200) for c in "abcde"]
    out = await run_v2(
        arv=_arv(comps),
        avm=Decimal(200000),
        pull=_pull(TIER1, comps),
        max_tier=1,
        pull_at_tier=TierPuller({}),
        arv_for=lambda p: _arv(list(p.comps), p.tier),
    )
    assert len(out.records) == 1 and out.penalised
    assert out.records[0].outputs == {
        "gap_pct": "0.3333",
        "confidence_before": "High",
        "confidence_after": "Medium",
    }


async def test_v2_without_avm_lowers_confidence_one_level() -> None:
    comps = [_comp(c, 150) for c in "abcde"]
    out = await run_v2(
        arv=_arv(comps),
        avm=None,
        pull=_pull(TIER1, comps),
        max_tier=3,
        pull_at_tier=TierPuller({}),
        arv_for=lambda p: _arv(list(p.comps), p.tier),
    )
    assert out.penalised and out.confidence is ArvConfidence.MEDIUM
    assert (
        out.records[0].result is VerificationResult.ADJUSTED
        and "AVM unavailable" in out.records[0].reason
    )


# -- V4 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remarks", "expected"),
    [
        (None, None),
        ("Well kept home", None),
        ("Sold as-is, needs TLC", ConditionClass.MEDIUM),
        ("Foundation movement noted; investor special", ConditionClass.HEAVY),
        ("Fire damage, uninhabitable", ConditionClass.GUT),
    ],
)
def test_evidence_condition_from_remarks(
    remarks: str | None, expected: ConditionClass | None
) -> None:
    worst, evidence = evidence_condition(
        year_built=1995, last_sale_date=date(2020, 1, 1), remarks=remarks, as_of=DEFAULT_TODAY
    )
    assert worst is expected
    assert bool(evidence) is (expected is not None)


def test_evidence_condition_old_home_long_hold_is_medium() -> None:
    worst, evidence = evidence_condition(
        year_built=1940, last_sale_date=date(1999, 1, 1), remarks=None, as_of=DEFAULT_TODAY
    )
    assert worst is ConditionClass.MEDIUM and "long hold" in evidence[0]
    worst2, _ = evidence_condition(
        year_built=1940, last_sale_date=date(2020, 1, 1), remarks=None, as_of=DEFAULT_TODAY
    )
    assert worst2 is None  # recent sale: no long-hold inference
    worst3, _ = evidence_condition(
        year_built=1940, last_sale_date=None, remarks=None, as_of=DEFAULT_TODAY
    )
    assert worst3 is ConditionClass.MEDIUM


def test_v4_bumps_one_level_only_and_infers_foundation() -> None:
    listing = ListingInfo("P", remarks="Fire damage; foundation shifted", photo_urls=("a", "b"))
    out = run_v4(
        intake_class=ConditionClass.LIGHT,
        disclosed_issues=[],
        year_built=1990,
        last_sale_date=None,
        listing=listing,
        as_of=DEFAULT_TODAY,
    )
    assert out.evidence_class is ConditionClass.GUT
    assert out.condition_class is ConditionClass.MEDIUM  # one level, not straight to Gut
    assert out.foundation_inferred and out.photo_count == 2
    assert (
        out.record.result is VerificationResult.ADJUSTED and "bumped one level" in out.record.reason
    )
    assert out.record.outputs["final_class"] == "Medium"


def test_v4_passes_when_evidence_not_worse() -> None:
    out = run_v4(
        intake_class=ConditionClass.HEAVY,
        disclosed_issues=["foundation"],
        year_built=1990,
        last_sale_date=None,
        listing=ListingInfo("P", remarks="as-is, foundation work needed"),
        as_of=DEFAULT_TODAY,
    )
    assert (
        out.condition_class is ConditionClass.HEAVY and out.record.result is VerificationResult.PASS
    )
    assert not out.foundation_inferred  # already disclosed; the line item handles it


def test_v4_without_listing() -> None:
    out = run_v4(
        intake_class=ConditionClass.MEDIUM,
        disclosed_issues=[],
        year_built=2001,
        last_sale_date=None,
        listing=None,
        as_of=DEFAULT_TODAY,
    )
    assert out.photo_count == 0 and out.record.result is VerificationResult.PASS
    assert out.record.inputs["listing"] is None


# -- V6 rule + PassRecord -------------------------------------------------------------------------


def test_drift_rule_is_five_percent() -> None:
    assert drift_exceeds(Decimal(200000), Decimal(210000)) == (False, Decimal("0.0476"))
    assert drift_exceeds(Decimal(200000), Decimal(211000)) == (True, Decimal("0.0521"))


def test_pass_record_round_trip() -> None:
    rec = PassRecord(
        VerificationPass.V6_PRE_SEND_DRIFT,
        2,
        {"a": 1},
        {"b": "x"},
        VerificationResult.ADJUSTED,
        "why",
    )
    doc = rec.to_json()
    assert doc["pass"] == "V6" and PassRecord.from_json(doc) == rec
