"""The A7 offer pipeline (REQUIREMENTS.md A7-01..A7-10, A7-18; ENG-Q10, ENG-Q11).

``A7OfferEngine.compute`` runs steps 1-7 plus the step-8 draft on an in-memory
``OfferComputation``; ``pre_send_check`` runs V6 immediately before a send. The engine does no
I/O beyond the property provider and the comp-audit model: persistence (offer row, verification
rows, QA review, policy decision, Graph send) is the activity layer's job
(``activities/offers.py``).

| step | what                                   | verification after it           |
|------|----------------------------------------|---------------------------------|
| 1    | address -> parcel facts (county+provider) | V1 dual-source facts         |
| 2    | tiered comp pull                       |                                 |
| 3    | ARV (trim, weight, x sqft)             | V3 comp audit, then V2 vs AVM   |
| 4    | repairs                                | V4 repair cross-check           |
| 5    | MAO                                    |                                 |
| 6    | offer (minus room, round down $500)    | V5 independent recompute        |
| 7    | gates                                  |                                 |
| 8    | draft from the locked template         | (A5 QA in the activity)         |
| 9    | send per policy                        | V6 pre-send drift + gates again |
| 10   | hand to S5                             |                                 |

Any ``escalate`` outcome forces ARV confidence to Low so gate 1 fails (A7-04 "Low -> human");
a V5 mismatch sets ``blocked_reason`` and the activity blocks the offer and raises an alert.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from wholesaling.agents.a7.arv import ArvEstimate, compute_arv, lower_confidence
from wholesaling.agents.a7.audit import CompAuditResult, prefilter_flags
from wholesaling.agents.a7.comps import CompPull, CompTier, pull_comps, tiers_from_config
from wholesaling.agents.a7.gates import (
    FlagInputs,
    GateInputs,
    GateResult,
    all_passed,
    derive_open_flags,
    evaluate_gates,
    failed_names,
)
from wholesaling.agents.a7.mao import MaoInputs, MaoResult, compute_mao
from wholesaling.agents.a7.repairs import RepairEstimate, estimate_repairs
from wholesaling.agents.a7.templates import RenderedDraft, check_offer_body, render_offer
from wholesaling.agents.a7.verification import (
    PassRecord,
    ResolvedFacts,
    drift_exceeds,
    run_v1,
    run_v2,
    run_v3,
    run_v4,
    run_v5,
)
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.config.tunables import A7Config, DealBand
from wholesaling.db.models.enums import (
    ArvConfidence,
    ConditionClass,
    VerificationPass,
    VerificationResult,
)
from wholesaling.integrations.data.property import (
    ListingInfo,
    PropertyAddress,
    PropertyDataProvider,
    SoldComp,
)

STATUS_READY = "ready"  # every pass and gate passed: eligible for L1 auto-send
STATUS_ESCALATE = "escalate"  # a human decides (Low confidence / failed gate / escalated pass)
STATUS_BLOCKED = "blocked"  # V5 mismatch (code bug) or nothing to offer

ESCALATE_PHOTOS_NOTE = "no photos supplied: confidence dropped one level (SEL-06)"


class CompAuditor(Protocol):
    async def audit(
        self, subject: dict[str, Any], comps: Sequence[SoldComp], *, as_of: date | None = None
    ) -> CompAuditResult: ...


class PrefilterAuditor:
    """Deterministic auditor (no model): the pre-filters alone. Used when no runner is wired."""

    async def audit(
        self, subject: dict[str, Any], comps: Sequence[SoldComp], *, as_of: date | None = None
    ) -> CompAuditResult:
        return CompAuditResult(
            verdicts=prefilter_flags(comps), run_id=None, model=None, rationale="prefilter only"
        )


@dataclass(slots=True)
class SubjectInput:
    """Everything step 1 needs plus the intake answers the later steps and gates use."""

    address: PropertyAddress
    market_name: str
    condition_class: ConditionClass
    disclosed_issues: list[str] = field(default_factory=list)
    asking_price: Decimal | None = None
    photo_count: int = 0
    seller_name: str | None = None
    seller_email: str | None = None
    motivation: str | None = None
    liens: str | None = None
    liens_unknown: bool = True
    hoa: str | None = None
    hoa_unknown: bool = True
    co_owners: str | None = None
    co_owners_unknown: bool = True
    seller_suppressed: bool = False
    active_offer_parcel_ids: frozenset[str] = frozenset()
    market_hard_cap: Decimal | None = None
    market_pct: Decimal | None = None
    closing_cost_pct: Decimal | None = None
    repair_rate_override: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "address": self.address.display,
            "market_name": self.market_name,
            "condition_class": self.condition_class.value,
            "disclosed_issues": list(self.disclosed_issues),
            "asking_price": str(self.asking_price) if self.asking_price is not None else None,
            "photo_count": self.photo_count,
            "seller_name": self.seller_name,
            "motivation": self.motivation,
            "liens": self.liens,
            "liens_unknown": self.liens_unknown,
            "hoa": self.hoa,
            "hoa_unknown": self.hoa_unknown,
            "co_owners": self.co_owners,
            "co_owners_unknown": self.co_owners_unknown,
            "seller_suppressed": self.seller_suppressed,
            "market_hard_cap": str(self.market_hard_cap) if self.market_hard_cap else None,
        }


@dataclass(slots=True)
class OfferComputation:
    """The full calculation record (A7-18) as it builds up through the steps."""

    subject: SubjectInput
    as_of: date
    facts: ResolvedFacts | None = None
    pull: CompPull | None = None
    comps: list[SoldComp] = field(default_factory=list)
    comps_rejected: list[dict[str, Any]] = field(default_factory=list)
    arv: ArvEstimate | None = None
    arv_confidence: ArvConfidence = ArvConfidence.LOW
    confidence_notes: list[str] = field(default_factory=list)
    avm: Decimal | None = None
    listing: ListingInfo | None = None
    condition_class: ConditionClass | None = None
    repairs: RepairEstimate | None = None
    mao: MaoResult | None = None
    gates: list[GateResult] = field(default_factory=list)
    open_flags: list[str] = field(default_factory=list)
    verifications: list[PassRecord] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    draft: RenderedDraft | None = None
    template_violations: list[str] = field(default_factory=list)
    hard_cap: Decimal | None = None

    @property
    def status(self) -> str:
        if self.blocked_reason:
            return STATUS_BLOCKED
        if self.escalations or not all_passed(self.gates):
            return STATUS_ESCALATE
        return STATUS_READY

    @property
    def gates_passed(self) -> bool:
        return self.status == STATUS_READY

    @property
    def offer_price(self) -> Decimal | None:
        return self.mao.offer if self.mao else None

    @property
    def parcel_id(self) -> str | None:
        return self.facts.parcel_id if self.facts else None

    @classmethod
    def restore(cls, subject: SubjectInput, record: dict[str, Any]) -> OfferComputation:
        """Rehydrate from ``offers.calc_record`` so V6 can run in a later activity. Derived
        values (ARV, MAO, gates) are re-derived from the stored comps and inputs; stored
        verification rows, rejections and notes are carried as they were."""
        as_of = date.fromisoformat(str(record["as_of"]))
        comp = cls(subject=subject, as_of=as_of)
        comp.hard_cap = Decimal(str(record["hard_cap"])) if record.get("hard_cap") else None
        comp.facts = ResolvedFacts.from_json(record["facts"]) if record.get("facts") else None
        comp.comps = [SoldComp.from_json(c) for c in record.get("comps_kept") or []]
        comp.comps_rejected = [dict(r) for r in record.get("comps_rejected") or []]
        comp.pull = (
            CompPull.from_json(record["comp_pull"], comp.comps) if record.get("comp_pull") else None
        )
        comp.arv_confidence = ArvConfidence(str(record.get("arv_confidence", "Low")))
        comp.confidence_notes = [str(n) for n in record.get("confidence_notes") or []]
        comp.avm = Decimal(str(record["arv_avm"])) if record.get("arv_avm") else None
        comp.listing = ListingInfo.from_json(record["listing"]) if record.get("listing") else None
        comp.condition_class = (
            ConditionClass(str(record["condition_class_final"]))
            if record.get("condition_class_final")
            else None
        )
        comp.repairs = (
            RepairEstimate.from_json(record["repairs"]) if record.get("repairs") else None
        )
        comp.open_flags = [str(f) for f in record.get("open_flags") or []]
        comp.verifications = [PassRecord.from_json(v) for v in record.get("verifications") or []]
        comp.escalations = [str(e) for e in record.get("escalations") or []]
        comp.blocked_reason = record.get("blocked_reason")
        comp.template_violations = [str(t) for t in record.get("template_violations") or []]
        if comp.pull is not None and comp.comps and comp.facts and comp.facts.sqft:
            comp.arv = compute_arv(
                comp.comps, subject_sqft=comp.facts.sqft, tier=comp.pull.tier, as_of=as_of
            )
        mao_json = record.get("mao")
        if mao_json:
            comp.mao = compute_mao(MaoInputs.from_json(mao_json["inputs"]))
        gates_json = record.get("gates") or []
        comp.gates = [
            GateResult(
                name=str(g["name"]),
                passed=bool(g["passed"]),
                value=g.get("value"),
                threshold=g.get("threshold"),
                detail=str(g.get("detail", "")),
            )
            for g in gates_json
        ]
        draft = record.get("draft")
        if draft:
            comp.draft = RenderedDraft(
                subject=str(draft["subject"]),
                body=str(draft["body"]),
                template_version=str(draft.get("template_version", "")),
                variables=dict(draft.get("variables") or {}),
            )
        return comp

    def record(self, rec: PassRecord) -> None:
        self.verifications.append(rec)
        if rec.result is VerificationResult.ESCALATE:
            self.escalations.append(f"{rec.pass_id.value}: {rec.reason}")

    def next_iteration(self, pass_id: VerificationPass) -> int:
        return 1 + sum(1 for v in self.verifications if v.pass_id is pass_id)

    def calc_record(self) -> dict[str, Any]:
        """What ``offers.calc_record`` stores: every input, intermediate and outcome."""
        return {
            "as_of": self.as_of.isoformat(),
            "subject": self.subject.to_json(),
            "sources_used": list(self.facts.sources_used) if self.facts else [],
            "facts": self.facts.to_json() if self.facts else None,
            "comp_pull": self.pull.to_json() if self.pull else None,
            "comps_kept": [c.to_json() for c in self.comps],
            "comps_rejected": list(self.comps_rejected),
            "arv_comps": self.arv.to_json() if self.arv else None,
            "arv_avm": str(self.avm) if self.avm is not None else None,
            "arv_confidence": self.arv_confidence.value,
            "confidence_notes": list(self.confidence_notes),
            "listing": self.listing.to_json() if self.listing else None,
            "condition_class_intake": self.subject.condition_class.value,
            "condition_class_final": self.condition_class.value if self.condition_class else None,
            "repairs": self.repairs.to_json() if self.repairs else None,
            "mao": self.mao.to_json() if self.mao else None,
            "hard_cap": str(self.hard_cap) if self.hard_cap is not None else None,
            "open_flags": list(self.open_flags),
            "gates": [g.to_json() for g in self.gates],
            "gates_passed": all_passed(self.gates),
            "gates_failed": failed_names(self.gates),
            "verifications": [v.to_json() for v in self.verifications],
            "escalations": list(self.escalations),
            "blocked_reason": self.blocked_reason,
            "status": self.status,
            "draft": self.draft.to_json() if self.draft else None,
            "template_violations": list(self.template_violations),
        }


class A7OfferEngine:
    def __init__(
        self,
        provider: PropertyDataProvider,
        config: A7Config,
        *,
        deal_band: DealBand,
        identity: SenderIdentity,
        auditor: CompAuditor | None = None,
        today: date | None = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self.deal_band = deal_band
        self.identity = identity
        self.auditor: CompAuditor = auditor or PrefilterAuditor()
        self.today = today or date.today()
        self.tiers: list[CompTier] = tiers_from_config(config)

    # -- helpers ---------------------------------------------------------------------------------

    def _arv_for(self, comps: list[SoldComp], sqft: int, tier: CompTier) -> ArvEstimate | None:
        if not comps:
            return None
        return compute_arv(comps, subject_sqft=sqft, tier=tier, as_of=self.today)

    async def _pull(
        self, comp: OfferComputation, *, start_tier: int, exclude: Sequence[str]
    ) -> CompPull:
        facts = comp.facts
        assert facts is not None and facts.sqft is not None
        return await pull_comps(
            self.provider,
            postal_code=comp.subject.address.postal_code or "",
            parcel_id=facts.parcel_id,
            subject_sqft=facts.sqft,
            subject_year_built=facts.year_built,
            tiers=self.tiers,
            as_of=self.today,
            start_tier=start_tier,
            exclude_ids=exclude,
        )

    def _rejected_ids(self, comp: OfferComputation) -> list[str]:
        return [str(r["comp"]["comp_id"]) for r in comp.comps_rejected]

    def _subject_summary(self, comp: OfferComputation) -> dict[str, Any]:
        facts = comp.facts
        return {
            "address": comp.subject.address.display,
            "sqft": facts.sqft if facts else None,
            "year_built": facts.year_built if facts else None,
            "beds": facts.beds if facts else None,
            "baths": str(facts.baths) if facts and facts.baths is not None else None,
            "condition_class": comp.subject.condition_class.value,
        }

    def _apply_confidence(self, comp: OfferComputation, base: ArvConfidence) -> None:
        confidence = base
        if comp.subject.photo_count == 0:
            confidence = lower_confidence(confidence)
            if ESCALATE_PHOTOS_NOTE not in comp.confidence_notes:
                comp.confidence_notes.append(ESCALATE_PHOTOS_NOTE)
        if comp.escalations:
            confidence = ArvConfidence.LOW
            note = "verification escalated: confidence forced to Low (human decides)"
            if note not in comp.confidence_notes:
                comp.confidence_notes.append(note)
        comp.arv_confidence = confidence

    def _mao(self, comp: OfferComputation) -> MaoResult:
        assert comp.arv is not None and comp.repairs is not None
        return compute_mao(
            MaoInputs.from_config(
                self.config,
                arv=comp.arv.arv,
                repairs=comp.repairs.total,
                market_pct=comp.subject.market_pct,
                closing_cost_pct=comp.subject.closing_cost_pct,
            )
        )

    def _gates(self, comp: OfferComputation) -> list[GateResult]:
        assert comp.arv is not None and comp.mao is not None
        parcel = comp.parcel_id
        return evaluate_gates(
            self.config,
            GateInputs(
                arv=comp.arv.arv,
                arv_confidence=comp.arv_confidence,
                offer=comp.mao.offer,
                hard_cap=comp.hard_cap
                if comp.hard_cap is not None
                else self.deal_band.max_purchase,
                asking_price=comp.subject.asking_price,
                open_flags=list(comp.open_flags),
                seller_suppressed=comp.subject.seller_suppressed,
                active_offer_on_parcel=bool(
                    parcel and parcel in comp.subject.active_offer_parcel_ids
                ),
            ),
        )

    def _draft(self, comp: OfferComputation) -> None:
        assert comp.mao is not None
        comp.draft = render_offer(
            seller_name=comp.subject.seller_name,
            address=comp.subject.address.display,
            offer=comp.mao.offer,
            cash_close_days=int(self.config.cash_close_days),
            identity=self.identity,
        )
        comp.template_violations = check_offer_body(
            comp.draft.body, offer=comp.mao.offer, cash_close_days=int(self.config.cash_close_days)
        )
        if comp.template_violations:
            comp.blocked_reason = "offer template guard failed: " + ", ".join(
                comp.template_violations
            )

    # -- steps -----------------------------------------------------------------------------------

    async def compute(self, subject: SubjectInput) -> OfferComputation:
        comp = OfferComputation(subject=subject, as_of=self.today)
        comp.hard_cap = (
            subject.market_hard_cap
            if subject.market_hard_cap is not None
            else Decimal(str(self.deal_band.max_purchase))
        )

        # Step 1 + V1
        facts, v1 = await run_v1(self.provider, subject.address)
        comp.facts = facts
        comp.record(v1)
        if facts is None or not facts.sqft:
            comp.blocked_reason = "property could not be resolved (no sqft from any source)"
            comp.arv_confidence = ArvConfidence.LOW
            return comp
        comp.open_flags = derive_open_flags(
            FlagInputs(
                liens=subject.liens,
                liens_unknown=subject.liens_unknown,
                hoa=subject.hoa,
                hoa_unknown=subject.hoa_unknown,
                co_owners=subject.co_owners,
                co_owners_unknown=subject.co_owners_unknown,
                motivation=subject.motivation,
                disclosed_issues=list(subject.disclosed_issues),
                property_type=facts.property_type,
                units=facts.units,
            )
        )
        sqft = facts.sqft

        # Step 2
        pull = await self._pull(comp, start_tier=1, exclude=())
        comp.pull = pull
        comp.comps = list(pull.comps)

        # Step 3
        comp.arv = self._arv_for(comp.comps, sqft, pull.tier)

        # V3 then V2
        async def _audit(comps: Sequence[SoldComp]) -> CompAuditResult:
            return await self.auditor.audit(self._subject_summary(comp), comps, as_of=self.today)

        v3 = await run_v3(
            _audit,
            comp.comps,
            comp.arv,
            lambda cs: self._arv_for(cs, sqft, pull.tier),
        )
        comp.comps = v3.comps
        comp.comps_rejected.extend(v3.rejected)
        comp.arv = v3.arv
        for rec in v3.records:
            comp.record(rec)

        comp.avm = await self.provider.avm(facts.parcel_id) if facts.parcel_id else None
        rejected = self._rejected_ids(comp)

        async def _pull_at(tier: int) -> CompPull:
            return await self._pull(comp, start_tier=tier, exclude=rejected)

        v2 = await run_v2(
            arv=comp.arv,
            avm=comp.avm,
            pull=pull,
            max_tier=max(t.tier for t in self.tiers),
            pull_at_tier=_pull_at,
            arv_for=lambda p: self._arv_for(list(p.comps), sqft, p.tier),
        )
        comp.pull = v2.pull
        comp.comps = list(v2.pull.comps) if v2.pull is not pull else comp.comps
        comp.arv = v2.arv
        for rec in v2.records:
            comp.record(rec)
        if v2.penalised:
            comp.confidence_notes.append("V2: confidence lowered one level (ARV vs AVM gap)")
        if comp.arv is None:
            comp.record(
                PassRecord(
                    VerificationPass.V2_DUAL_METHOD_ARV,
                    comp.next_iteration(VerificationPass.V2_DUAL_METHOD_ARV),
                    {"comps": [c.comp_id for c in comp.comps]},
                    {},
                    VerificationResult.ESCALATE,
                    "no usable comps remain; ARV cannot be estimated",
                )
            )
            comp.blocked_reason = "no usable comps: ARV cannot be estimated"
            self._apply_confidence(comp, ArvConfidence.LOW)
            return comp
        self._apply_confidence(comp, v2.confidence)

        # Step 4 + V4
        comp.listing = (
            await self.provider.listing_info(facts.parcel_id) if facts.parcel_id else None
        )
        v4 = run_v4(
            intake_class=subject.condition_class,
            disclosed_issues=subject.disclosed_issues,
            year_built=facts.year_built,
            last_sale_date=facts.last_sale_date,
            listing=comp.listing,
            as_of=self.today,
        )
        comp.record(v4.record)
        comp.condition_class = v4.condition_class
        comp.repairs = estimate_repairs(
            self.config,
            sqft=sqft,
            condition_class=v4.condition_class,
            disclosed_issues=subject.disclosed_issues,
            market_name=subject.market_name,
            city=subject.address.city,
            foundation_inferred=v4.foundation_inferred,
            rate_override=subject.repair_rate_override,
        )

        # Steps 5-6 + V5
        comp.mao = self._mao(comp)
        ok, v5 = run_v5(comp.mao)
        comp.record(v5)
        if not ok:
            comp.blocked_reason = f"V5 recompute mismatch: {v5.reason}"
            return comp

        # Step 7
        comp.gates = self._gates(comp)

        # Step 8 (draft; QA runs in the activity)
        self._draft(comp)
        return comp

    async def pre_send_check(self, comp: OfferComputation) -> OfferComputation:
        """V6 (A7-17): re-pull comps at the tier used, excluding audited-out comps and any new
        comp the deterministic pre-filters reject; if the ARV moved >5%, recompute MAO, offer,
        V5 and the draft. The gates are re-run either way."""
        if comp.blocked_reason or comp.arv is None or comp.pull is None or comp.facts is None:
            return comp
        sqft = comp.facts.sqft
        assert sqft
        iteration = comp.next_iteration(VerificationPass.V6_PRE_SEND_DRIFT)
        rejected = set(self._rejected_ids(comp))
        fresh = await self._pull(comp, start_tier=comp.pull.tier.tier, exclude=sorted(rejected))
        known = {c.comp_id for c in comp.comps}
        new_comps = [c for c in fresh.comps if c.comp_id not in known]
        prefiltered = prefilter_flags(fresh.comps) if new_comps else {}
        auto_rejected = [
            c for c in new_comps if c.comp_id in prefiltered and prefiltered[c.comp_id].flagged
        ]
        usable = [c for c in fresh.comps if c.comp_id not in {r.comp_id for r in auto_rejected}]
        new_arv = self._arv_for(usable, sqft, fresh.tier) if usable else None
        old_arv = comp.arv.arv
        inputs = {
            "tier": fresh.tier.tier,
            "arv_before": str(old_arv),
            "comp_ids_before": sorted(known),
            "comp_ids_now": [c.comp_id for c in usable],
            "new_comp_ids": [c.comp_id for c in new_comps],
            "auto_rejected": [
                {"comp_id": c.comp_id, "flags": prefiltered[c.comp_id].flags} for c in auto_rejected
            ],
            "gates_before": [g.to_json() for g in comp.gates],
        }
        if new_arv is None:
            comp.record(
                PassRecord(
                    VerificationPass.V6_PRE_SEND_DRIFT,
                    iteration,
                    inputs,
                    {},
                    VerificationResult.ESCALATE,
                    "re-pull returned no usable comps; human must review before send",
                )
            )
            self._apply_confidence(comp, comp.arv_confidence)
            comp.gates = self._gates(comp)
            return comp
        exceeded, gap = drift_exceeds(old_arv, new_arv.arv)
        for c in auto_rejected:
            comp.comps_rejected.append(
                {
                    "comp": c.to_json(),
                    "flags": list(prefiltered[c.comp_id].flags),
                    "reasons": list(prefiltered[c.comp_id].reasons),
                    "stage": "V6",
                }
            )
        if exceeded:
            comp.pull = fresh
            comp.comps = usable
            comp.arv = new_arv
            # Confidence from the new comp set, then the same penalties as before.
            base = new_arv.confidence
            if any("V2: confidence lowered" in n for n in comp.confidence_notes):
                base = lower_confidence(base)
            self._apply_confidence(comp, base)
            comp.mao = self._mao(comp)
            ok, v5 = run_v5(
                comp.mao, iteration=comp.next_iteration(VerificationPass.V5_INDEPENDENT_RECOMPUTE)
            )
            comp.record(v5)
            if not ok:
                comp.blocked_reason = f"V5 recompute mismatch after V6: {v5.reason}"
            comp.gates = self._gates(comp)
            self._draft(comp)
            comp.record(
                PassRecord(
                    VerificationPass.V6_PRE_SEND_DRIFT,
                    iteration,
                    inputs,
                    {
                        "arv_after": str(new_arv.arv),
                        "gap_pct": str(gap),
                        "offer_after": str(comp.mao.offer),
                        "gates_after": [g.to_json() for g in comp.gates],
                        "gates_passed": all_passed(comp.gates),
                    },
                    VerificationResult.ADJUSTED,
                    f"new comps moved ARV {gap:.1%} (> 5%); "
                    f"recomputed offer {comp.mao.offer} and re-ran gates",
                )
            )
            return comp
        comp.gates = self._gates(comp)
        comp.record(
            PassRecord(
                VerificationPass.V6_PRE_SEND_DRIFT,
                iteration,
                inputs,
                {
                    "arv_after": str(new_arv.arv),
                    "gap_pct": str(gap),
                    "gates_after": [g.to_json() for g in comp.gates],
                    "gates_passed": all_passed(comp.gates),
                },
                VerificationResult.PASS,
                f"ARV drift {gap:.1%} within 5%; gates re-run",
            )
        )
        return comp


__all__ = [
    "ESCALATE_PHOTOS_NOTE",
    "STATUS_BLOCKED",
    "STATUS_ESCALATE",
    "STATUS_READY",
    "A7OfferEngine",
    "CompAuditor",
    "OfferComputation",
    "PrefilterAuditor",
    "SubjectInput",
]
