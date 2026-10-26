"""Verification passes V1..V6 (REQUIREMENTS.md A7-11..A7-18, ENG-Q11).

Each pass is a function that returns one or more ``PassRecord``s - the rows the activity writes
to ``offer_verifications`` (pass, iteration, inputs, outputs, result, reason). Loops are bounded
by constants here (V2 <= 3 passes, V3 <= 2 iterations) so the workflow always terminates (A7-11).

Order (ENG-Q11): V1 after step 1; V3 then V2 after step 3; V4 after step 4; V5 after step 6;
gates at 7; QA at 8; V6 immediately before send with the gates re-run. The engine
(``engine.py``) sequences them; this module holds the rules.

Outcomes: ``pass`` (nothing changed), ``adjusted`` (the pass changed a number or a class and
says how), ``escalate`` (a human decides: the engine forces ARV confidence to Low so gate 1
fails), ``fail`` (V5 only: a code bug; the offer is blocked and an alert is raised).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from wholesaling.agents.a7.arv import ArvEstimate, lower_confidence, pct_gap
from wholesaling.agents.a7.audit import CompAuditResult
from wholesaling.agents.a7.comps import CompPull
from wholesaling.agents.a7.mao import MaoResult
from wholesaling.agents.a7.recompute import check_against
from wholesaling.agents.a7.repairs import CLASS_ORDER, bump_class, class_rank
from wholesaling.db.models.enums import (
    ArvConfidence,
    ConditionClass,
    VerificationPass,
    VerificationResult,
)
from wholesaling.integrations.data.property import (
    FACT_FIELDS,
    ListingInfo,
    ParcelFacts,
    PropertyAddress,
    PropertyDataProvider,
    SoldComp,
)

FACT_GAP_PCT = Decimal("0.10")  # V1 (A7-12)
AVM_GAP_PCT = Decimal("0.10")  # V2 (A7-13)
V2_MAX_PASSES = 3
V3_MAX_ITERATIONS = 2
DRIFT_PCT = Decimal("0.05")  # V6 (A7-17)
LONG_HOLD_YEARS = 20
OLD_HOME_YEAR = 1950

_Q4 = Decimal("0.0001")


@dataclass(slots=True)
class PassRecord:
    pass_id: VerificationPass
    iteration: int
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    result: VerificationResult
    reason: str
    agent_run_id: uuid.UUID | None = None

    @property
    def escalated(self) -> bool:
        return self.result is VerificationResult.ESCALATE

    def to_json(self) -> dict[str, Any]:
        return {
            "pass": self.pass_id.value,
            "iteration": self.iteration,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "result": self.result.value,
            "reason": self.reason,
            "agent_run_id": str(self.agent_run_id) if self.agent_run_id else None,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PassRecord:
        run_id = d.get("agent_run_id")
        return cls(
            pass_id=VerificationPass(str(d["pass"])),
            iteration=int(d["iteration"]),
            inputs=dict(d.get("inputs") or {}),
            outputs=dict(d.get("outputs") or {}),
            result=VerificationResult(str(d["result"])),
            reason=str(d.get("reason") or ""),
            agent_run_id=uuid.UUID(str(run_id)) if run_id else None,
        )


# --------------------------------------------------------------------------------------------
# V1 - dual-source facts (A7-12)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ResolvedFacts:
    parcel_id: str | None
    sqft: int | None
    beds: int | None
    baths: Decimal | None
    year_built: int | None
    lot_sqft: int | None
    property_type: str
    units: int
    last_sale_date: date | None
    last_sale_price: Decimal | None
    owner_name: str | None
    field_sources: dict[str, str] = field(default_factory=dict)  # fact -> source that won
    sources_used: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["baths"] = str(self.baths) if self.baths is not None else None
        d["last_sale_date"] = self.last_sale_date.isoformat() if self.last_sale_date else None
        d["last_sale_price"] = str(self.last_sale_price) if self.last_sale_price else None
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ResolvedFacts:
        return cls(
            parcel_id=d.get("parcel_id"),
            sqft=d.get("sqft"),
            beds=d.get("beds"),
            baths=Decimal(str(d["baths"])) if d.get("baths") is not None else None,
            year_built=d.get("year_built"),
            lot_sqft=d.get("lot_sqft"),
            property_type=str(d.get("property_type", "sfr")),
            units=int(d.get("units", 1)),
            last_sale_date=date.fromisoformat(d["last_sale_date"])
            if d.get("last_sale_date")
            else None,
            last_sale_price=Decimal(str(d["last_sale_price"]))
            if d.get("last_sale_price")
            else None,
            owner_name=d.get("owner_name"),
            field_sources=dict(d.get("field_sources") or {}),
            sources_used=[str(x) for x in d.get("sources_used") or []],
            unresolved=[str(x) for x in d.get("unresolved") or []],
        )


def _num(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def facts_agree(a: Any, b: Any, tolerance: Decimal = FACT_GAP_PCT) -> bool:
    """Two values of one fact agree when both exist and differ by <= 10% of the larger."""
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return False
    return pct_gap(x, y) <= tolerance


def _get(facts: ParcelFacts | None, name: str) -> Any:
    return getattr(facts, name) if facts is not None else None


async def run_v1(
    provider: PropertyDataProvider, address: PropertyAddress
) -> tuple[ResolvedFacts | None, PassRecord]:
    """County vs provider on sqft/beds/baths/year/lot; >10% gap pulls the third source;
    majority wins; no majority -> escalate."""
    county = await provider.county_facts(address)
    prov = await provider.provider_facts(address)
    inputs: dict[str, Any] = {
        "address": address.display,
        "county": county.to_json() if county else None,
        "provider": prov.to_json() if prov else None,
    }
    if county is None and prov is None:
        third = await provider.third_source_facts(address)
        inputs["third"] = third.to_json() if third else None
        if third is None:
            return None, PassRecord(
                VerificationPass.V1_DUAL_SOURCE_FACTS,
                1,
                inputs,
                {"resolved": None},
                VerificationResult.ESCALATE,
                "no parcel facts from any source; address could not be resolved",
            )
        # One source only: usable for the record, but nothing corroborates it.
        resolved = _resolved_from(third, {f: third.source for f in FACT_FIELDS}, [third.source])
        resolved.unresolved = list(FACT_FIELDS)
        return resolved, PassRecord(
            VerificationPass.V1_DUAL_SOURCE_FACTS,
            1,
            inputs,
            {"resolved": resolved.to_json()},
            VerificationResult.ESCALATE,
            "only one source has facts for this address; dual-source check impossible",
        )

    disagreeing = [f for f in FACT_FIELDS if not facts_agree(_get(county, f), _get(prov, f))]
    parcel_mismatch = (
        county is not None
        and prov is not None
        and county.parcel_id
        and prov.parcel_id
        and county.parcel_id != prov.parcel_id
    )
    primary = county or prov
    assert primary is not None
    if not disagreeing and not parcel_mismatch:
        agreed = {f: primary.source for f in FACT_FIELDS}
        resolved = _resolved_from(primary, agreed, [s.source for s in (county, prov) if s])
        return resolved, PassRecord(
            VerificationPass.V1_DUAL_SOURCE_FACTS,
            1,
            inputs,
            {"resolved": resolved.to_json(), "disagreeing": []},
            VerificationResult.PASS,
            "county and provider agree on sqft, beds, baths, year built and lot within 10%",
        )

    third = await provider.third_source_facts(address)
    inputs["third"] = third.to_json() if third else None
    sources: dict[str, str] = {}
    values: dict[str, Any] = {}
    unresolved: list[str] = []
    for f in FACT_FIELDS:
        c, p, t = _get(county, f), _get(prov, f), _get(third, f)
        if f not in disagreeing:
            values[f], sources[f] = (c if c is not None else p), primary.source
            continue
        if facts_agree(c, t):
            values[f], sources[f] = c, f"{county.source if county else 'county'}+third"
        elif facts_agree(p, t):
            values[f], sources[f] = p, f"{prov.source if prov else 'provider'}+third"
        else:
            # No majority: keep the county value provisionally, mark unresolved.
            values[f] = c if c is not None else (p if p is not None else t)
            sources[f] = "unresolved"
            unresolved.append(f)
    if parcel_mismatch:
        unresolved.append("parcel_id")
    used = [s.source for s in (county, prov, third) if s is not None]
    resolved = ResolvedFacts(
        parcel_id=primary.parcel_id,
        sqft=values["sqft"],
        beds=values["beds"],
        baths=_num(values["baths"]),
        year_built=values["year_built"],
        lot_sqft=values["lot_sqft"],
        property_type=primary.property_type,
        units=primary.units,
        last_sale_date=primary.last_sale_date or _get(prov, "last_sale_date"),
        last_sale_price=primary.last_sale_price or _get(prov, "last_sale_price"),
        owner_name=primary.owner_name or _get(prov, "owner_name"),
        field_sources=sources,
        sources_used=used,
        unresolved=unresolved,
    )
    outputs = {"resolved": resolved.to_json(), "disagreeing": disagreeing}
    if unresolved:
        return resolved, PassRecord(
            VerificationPass.V1_DUAL_SOURCE_FACTS,
            1,
            inputs,
            outputs,
            VerificationResult.ESCALATE,
            f"no two sources agree on {', '.join(unresolved)}; human must confirm facts",
        )
    return resolved, PassRecord(
        VerificationPass.V1_DUAL_SOURCE_FACTS,
        1,
        inputs,
        outputs,
        VerificationResult.ADJUSTED,
        f"sources disagreed on {', '.join(disagreeing)}; third source resolved by majority",
    )


def _resolved_from(facts: ParcelFacts, sources: dict[str, str], used: list[str]) -> ResolvedFacts:
    return ResolvedFacts(
        parcel_id=facts.parcel_id,
        sqft=facts.sqft,
        beds=facts.beds,
        baths=facts.baths,
        year_built=facts.year_built,
        lot_sqft=facts.lot_sqft,
        property_type=facts.property_type,
        units=facts.units,
        last_sale_date=facts.last_sale_date,
        last_sale_price=facts.last_sale_price,
        owner_name=facts.owner_name,
        field_sources=sources,
        sources_used=used,
    )


# --------------------------------------------------------------------------------------------
# V3 - comp audit (A7-14)
# --------------------------------------------------------------------------------------------

Auditor = Callable[[Sequence[SoldComp]], Awaitable[CompAuditResult]]
ArvFn = Callable[[list[SoldComp]], ArvEstimate | None]


@dataclass(slots=True)
class V3Outcome:
    comps: list[SoldComp]
    rejected: list[dict[str, Any]]
    arv: ArvEstimate | None
    records: list[PassRecord]
    iterations: int


async def run_v3(
    audit: Auditor, comps: list[SoldComp], arv_before: ArvEstimate | None, recompute: ArvFn
) -> V3Outcome:
    """Audit, remove flagged, recompute; loop until no new flags or ``V3_MAX_ITERATIONS``."""
    current = list(comps)
    arv = arv_before
    rejected: list[dict[str, Any]] = []
    records: list[PassRecord] = []
    iteration = 0
    while iteration < V3_MAX_ITERATIONS:
        iteration += 1
        result = await audit(current)
        flagged = [v for v in result.verdicts.values() if v.flagged]
        inputs = {
            "comp_ids": [c.comp_id for c in current],
            "arv_before": arv.to_json() if arv else None,
            "iteration_cap": V3_MAX_ITERATIONS,
        }
        if not flagged:
            records.append(
                PassRecord(
                    VerificationPass.V3_COMP_AUDIT,
                    iteration,
                    inputs,
                    {"audit": result.to_json(), "removed": [], "arv_after": inputs["arv_before"]},
                    VerificationResult.PASS,
                    "no comps flagged",
                    agent_run_id=result.run_id,
                )
            )
            break
        flagged_ids = {v.comp_id for v in flagged}
        removed = [c for c in current if c.comp_id in flagged_ids]
        current = [c for c in current if c.comp_id not in flagged_ids]
        for c in removed:
            v = result.verdicts[c.comp_id]
            rejected.append(
                {"comp": c.to_json(), "flags": list(v.flags), "reasons": list(v.reasons)}
            )
        arv = recompute(current) if current else None
        records.append(
            PassRecord(
                VerificationPass.V3_COMP_AUDIT,
                iteration,
                inputs,
                {
                    "audit": result.to_json(),
                    "removed": sorted(flagged_ids),
                    "remaining": [c.comp_id for c in current],
                    "arv_after": arv.to_json() if arv else None,
                },
                VerificationResult.ADJUSTED if current else VerificationResult.ESCALATE,
                (
                    f"removed {len(removed)} flagged comp(s): "
                    + "; ".join(
                        f"{c.comp_id}={','.join(result.verdicts[c.comp_id].flags)}" for c in removed
                    )
                    if current
                    else "every comp was flagged; no comps remain for an ARV"
                ),
                agent_run_id=result.run_id,
            )
        )
        if not current:
            break
    return V3Outcome(
        comps=current, rejected=rejected, arv=arv, records=records, iterations=iteration
    )


# --------------------------------------------------------------------------------------------
# V2 - dual-method ARV (A7-13)
# --------------------------------------------------------------------------------------------

PullAtTier = Callable[[int], Awaitable[CompPull]]
ArvForPull = Callable[[CompPull], ArvEstimate | None]


@dataclass(slots=True)
class V2Outcome:
    arv: ArvEstimate | None
    pull: CompPull
    confidence: ArvConfidence
    records: list[PassRecord]
    penalised: bool


async def run_v2(
    *,
    arv: ArvEstimate | None,
    avm: Decimal | None,
    pull: CompPull,
    max_tier: int,
    pull_at_tier: PullAtTier,
    arv_for: ArvForPull,
) -> V2Outcome:
    """Compare comp ARV to the AVM; >10% -> re-run comps at the next tier and compare again;
    still >10% after ``V2_MAX_PASSES`` (or no tier left) -> confidence minus one level."""
    records: list[PassRecord] = []
    current_arv, current_pull = arv, pull
    confidence = arv.confidence if arv else ArvConfidence.LOW
    if avm is None or avm <= 0:
        penal = lower_confidence(confidence)
        records.append(
            PassRecord(
                VerificationPass.V2_DUAL_METHOD_ARV,
                1,
                {"arv": arv.to_json() if arv else None, "avm": None, "tier": pull.tier.tier},
                {
                    "gap_pct": None,
                    "confidence_before": confidence.value,
                    "confidence_after": penal.value,
                },
                VerificationResult.ADJUSTED,
                "provider AVM unavailable; dual-method check impossible, "
                "confidence lowered one level",
            )
        )
        return V2Outcome(arv, pull, penal, records, penalised=True)

    for iteration in range(1, V2_MAX_PASSES + 1):
        comp_arv = current_arv.arv if current_arv else None
        gap = pct_gap(comp_arv, avm) if comp_arv is not None else None
        inputs = {
            "arv": current_arv.to_json() if current_arv else None,
            "avm": str(avm),
            "tier": current_pull.tier.tier,
            "pass_cap": V2_MAX_PASSES,
        }
        within = gap is not None and gap <= AVM_GAP_PCT
        if within:
            confidence = current_arv.confidence if current_arv else ArvConfidence.LOW
            records.append(
                PassRecord(
                    VerificationPass.V2_DUAL_METHOD_ARV,
                    iteration,
                    inputs,
                    {"gap_pct": str(gap), "confidence_after": confidence.value},
                    VerificationResult.PASS if iteration == 1 else VerificationResult.ADJUSTED,
                    (
                        f"comp ARV within {AVM_GAP_PCT:.0%} of AVM (gap {gap:.1%})"
                        if iteration == 1
                        else f"gap {gap:.1%} after re-running comps at tier "
                        f"{current_pull.tier.tier}"
                    ),
                )
            )
            return V2Outcome(current_arv, current_pull, confidence, records, penalised=False)

        next_tier = current_pull.tier.tier + 1
        can_expand = iteration < V2_MAX_PASSES and next_tier <= max_tier
        if not can_expand:
            base = current_arv.confidence if current_arv else ArvConfidence.LOW
            penal = lower_confidence(base)
            records.append(
                PassRecord(
                    VerificationPass.V2_DUAL_METHOD_ARV,
                    iteration,
                    inputs,
                    {
                        "gap_pct": str(gap) if gap is not None else None,
                        "confidence_before": base.value,
                        "confidence_after": penal.value,
                    },
                    VerificationResult.ADJUSTED,
                    (
                        f"comp ARV still >{AVM_GAP_PCT:.0%} from AVM "
                        f"(gap {gap:.1%}) after {iteration} pass(es); confidence lowered one level"
                        if gap is not None
                        else "no comp ARV to compare; confidence lowered one level"
                    ),
                )
            )
            return V2Outcome(current_arv, current_pull, penal, records, penalised=True)

        new_pull = await pull_at_tier(next_tier)
        new_arv = arv_for(new_pull)
        records.append(
            PassRecord(
                VerificationPass.V2_DUAL_METHOD_ARV,
                iteration,
                inputs,
                {
                    "gap_pct": str(gap) if gap is not None else None,
                    "action": f"re-ran comps at tier {new_pull.tier.tier}",
                    "arv_after": new_arv.to_json() if new_arv else None,
                },
                VerificationResult.ADJUSTED,
                f"comp ARV >{AVM_GAP_PCT:.0%} from AVM; re-running comps at tier "
                f"{new_pull.tier.tier}",
            )
        )
        current_pull, current_arv = new_pull, new_arv
    msg = "unreachable: V2 loop exits inside"  # pragma: no cover
    raise RuntimeError(msg)


# --------------------------------------------------------------------------------------------
# V4 - repair cross-check (A7-15)
# --------------------------------------------------------------------------------------------

_GUT_WORDS = re.compile(
    r"\b(fire damage|uninhabitable|condemned|gut(ted)?|down to the studs|flood(ed| damage)|"
    r"collapsed roof|no (roof|plumbing|electrical))\b",
    re.I,
)
_HEAVY_WORDS = re.compile(
    r"\b(foundation|roof leak|mold|investor special|needs work|major repairs?|structural|"
    r"termite|cash only|not financeable|hoarder|water damage)\b",
    re.I,
)
_MEDIUM_WORDS = re.compile(
    r"\b(as[- ]is|tlc|handyman|dated|original (kitchen|bath|condition)|needs updating|cosmetic|"
    r"deferred maintenance|fixer)\b",
    re.I,
)
_FOUNDATION = re.compile(r"\bfoundation\b", re.I)


@dataclass(slots=True)
class V4Outcome:
    condition_class: ConditionClass
    evidence_class: ConditionClass | None
    foundation_inferred: bool
    evidence: list[str]
    photo_count: int
    record: PassRecord


def evidence_condition(
    *,
    year_built: int | None,
    last_sale_date: date | None,
    remarks: str | None,
    as_of: date,
) -> tuple[ConditionClass | None, list[str]]:
    """The worst condition class the evidence supports, with the evidence lines."""
    evidence: list[str] = []
    worst: ConditionClass | None = None

    def _raise(to: ConditionClass, why: str) -> None:
        nonlocal worst
        evidence.append(f"{to.value}: {why}")
        if worst is None or class_rank(to) > class_rank(worst):
            worst = to

    text = remarks or ""
    if m := _GUT_WORDS.search(text):
        _raise(ConditionClass.GUT, f"remarks mention {m.group(0)!r}")
    if m := _HEAVY_WORDS.search(text):
        _raise(ConditionClass.HEAVY, f"remarks mention {m.group(0)!r}")
    if m := _MEDIUM_WORDS.search(text):
        _raise(ConditionClass.MEDIUM, f"remarks mention {m.group(0)!r}")
    long_hold = last_sale_date is None or (as_of.year - last_sale_date.year) >= LONG_HOLD_YEARS
    if year_built is not None and year_built < OLD_HOME_YEAR and long_hold:
        held = (
            "no recorded sale"
            if last_sale_date is None
            else f"last sale {last_sale_date.isoformat()}"
        )
        _raise(ConditionClass.MEDIUM, f"built {year_built} and {held} (long hold)")
    return worst, evidence


def run_v4(
    *,
    intake_class: ConditionClass,
    disclosed_issues: Sequence[str],
    year_built: int | None,
    last_sale_date: date | None,
    listing: ListingInfo | None,
    as_of: date,
) -> V4Outcome:
    remarks = listing.remarks if listing else None
    photo_count = len(listing.photo_urls) if listing else 0
    evidence_class, evidence = evidence_condition(
        year_built=year_built, last_sale_date=last_sale_date, remarks=remarks, as_of=as_of
    )
    disclosed_text = " ".join(disclosed_issues)
    foundation_inferred = bool(_FOUNDATION.search(remarks or "")) and not _FOUNDATION.search(
        disclosed_text
    )
    inputs = {
        "intake_class": intake_class.value,
        "disclosed_issues": list(disclosed_issues),
        "year_built": year_built,
        "last_sale_date": last_sale_date.isoformat() if last_sale_date else None,
        "listing": listing.to_json() if listing else None,
        "photo_count": photo_count,
    }
    final = intake_class
    if evidence_class is not None and class_rank(evidence_class) > class_rank(intake_class):
        final = bump_class(intake_class)
        reason = (
            f"evidence supports {evidence_class.value}, worse than stated {intake_class.value}; "
            f"class bumped one level to {final.value}"
        )
        result = VerificationResult.ADJUSTED
    else:
        reason = (
            f"evidence ({evidence_class.value if evidence_class else 'none'}) does not contradict "
            f"stated {intake_class.value}"
        )
        result = VerificationResult.PASS
    if foundation_inferred:
        reason += "; foundation inferred from remarks (contingency added)"
    record = PassRecord(
        VerificationPass.V4_REPAIR_CROSS_CHECK,
        1,
        inputs,
        {
            "evidence_class": evidence_class.value if evidence_class else None,
            "evidence": evidence,
            "final_class": final.value,
            "foundation_inferred": foundation_inferred,
            "class_order": [c.value for c in CLASS_ORDER],
        },
        result,
        reason,
    )
    return V4Outcome(final, evidence_class, foundation_inferred, evidence, photo_count, record)


# --------------------------------------------------------------------------------------------
# V5 - independent recompute (A7-16)
# --------------------------------------------------------------------------------------------


def run_v5(mao: MaoResult, *, iteration: int = 1) -> tuple[bool, PassRecord]:
    check = check_against(mao.inputs.to_json(), engine_mao=mao.mao, engine_offer=mao.offer)
    record = PassRecord(
        VerificationPass.V5_INDEPENDENT_RECOMPUTE,
        iteration,
        {
            "logged_inputs": mao.inputs.to_json(),
            "engine_mao": str(mao.mao),
            "engine_offer": str(mao.offer),
        },
        check.to_json(),
        VerificationResult.PASS if check.matches else VerificationResult.FAIL,
        check.detail if check.matches else f"{check.detail}; offer BLOCKED (code bug)",
    )
    return check.matches, record


# --------------------------------------------------------------------------------------------
# V6 - pre-send drift (A7-17): the rule; the engine does the re-pull and recompute
# --------------------------------------------------------------------------------------------


def drift_exceeds(old_arv: Decimal, new_arv: Decimal) -> tuple[bool, Decimal]:
    gap = pct_gap(old_arv, new_arv)
    return gap > DRIFT_PCT, gap


__all__ = [
    "AVM_GAP_PCT",
    "DRIFT_PCT",
    "FACT_GAP_PCT",
    "LONG_HOLD_YEARS",
    "OLD_HOME_YEAR",
    "V2_MAX_PASSES",
    "V3_MAX_ITERATIONS",
    "ArvFn",
    "Auditor",
    "PassRecord",
    "ResolvedFacts",
    "V2Outcome",
    "V3Outcome",
    "V4Outcome",
    "drift_exceeds",
    "evidence_condition",
    "facts_agree",
    "run_v1",
    "run_v2",
    "run_v3",
    "run_v4",
    "run_v5",
]
