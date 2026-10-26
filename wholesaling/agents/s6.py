"""S6 Disposition (REQUIREMENTS.md SEL-10, SEL-11, BB-09, BB-11, POL-03k, XC-07; build step BO-12).

Runs **after a human signed the contract** (``contracts.status = signed``; audit
``contract.signed``). It does three things, all deterministic and model-free except the A5 QA
review of each draft:

1. ``select_buyers``: who hears about the deal, in tier order.
   - Tier 1: investors behind **approved** D2 matches (``matches.review_status = approved``,
     BB-09) whose property candidate is this deal's property (same seller lead, or same
     address key / parcel). A match is the strongest evidence a buy box fits.
   - Tier 2 (fallback when tier 1 is empty, or to widen it): investors in the deal's market
     with a *current, active* buy box whose price band contains the purchase price and whose
     geography lists the property's ZIP (or lists no ZIPs at all, i.e. market-wide).
   Within a tier: investors with prior **closed** deals with us first (``contracts.status =
   closed`` where they were the buyer), then A1 priority ``score`` desc, then
   ``purchase_count_24m`` desc, then name for a stable order. Only investors with a usable email
   contact are returned; suppression is checked by the activity at send time.
2. ``draft`` the blast for one buyer from ``templates/disposition.py``: evidence summary from
   the offer's calculation record (ARV + confidence, repairs, comp tier, open flags) with no
   seller-agreement claim beyond "under contract" (the template refuses otherwise).
3. ``S6QaAgent``: A5's review with one documented exemption - the deterministic
   ``prohibited_claim:under_contract`` pre-check is lifted **only when the contract row is
   signed**, because A5's own rule reads "unless the facts given say so" and the signed row is
   that fact. Every other pre-check and the model review stay in force.

Sending, policy (``BUYER_DISPOSITION_BLAST``) and persistence are the activity's job
(``activities/disposition.py``). S6 never touches DocuSign and never moves money.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.a5 import (
    FLAG_PROHIBITED_PREFIX,
    QA_SCHEMA,
    A5QaAgent,
    QaInput,
    QaResult,
    run_prechecks,
)
from wholesaling.agents.llm.interface import StructuredOutputError
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.templates.disposition import DispositionCopy, render_blast
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.db.models.buy_boxes import BuyBox, SearchProfile
from wholesaling.db.models.deals import (
    Contract,
    ContractStatus,
    ContractType,
    Match,
    PropertyCandidate,
)
from wholesaling.db.models.enums import ContactChannel, MatchReviewStatus, QaVerdict
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.offers import Offer
from wholesaling.db.models.sellers import Intake, SellerLead

UNDER_CONTRACT_FLAG = f"{FLAG_PROHIBITED_PREFIX}under_contract"

TIER_APPROVED_MATCH = 1
TIER_BUY_BOX_FIT = 2


@dataclass(frozen=True, slots=True)
class BuyerCandidate:
    investor_id: uuid.UUID
    name: str
    first_name: str | None
    email: str
    tier: int
    closed_deals: int
    score: int
    purchase_count_24m: int
    reason: str
    match_id: uuid.UUID | None = None
    buy_box_id: uuid.UUID | None = None

    @property
    def sort_key(self) -> tuple[int, int, int, int, str]:
        return (self.tier, -self.closed_deals, -self.score, -self.purchase_count_24m, self.name)


@dataclass(slots=True)
class DealFacts:
    """Everything the template and the QA review need about the deal, from the records."""

    contract_id: uuid.UUID
    contract_signed: bool
    market: str
    property_address: str
    property_short: str
    postal_code: str | None
    purchase_price: Decimal | None
    assignment_fee: Decimal | None
    arv: Decimal | None
    arv_confidence: str | None
    repairs: Decimal | None
    comp_tier: int | None
    condition_class: str | None
    open_flags: list[str] = field(default_factory=list)
    property_facts: dict[str, Any] = field(default_factory=dict)

    @property
    def asking_price(self) -> Decimal | None:
        """What a buyer pays: contract price plus our assignment fee."""
        if self.purchase_price is None:
            return None
        return self.purchase_price + (self.assignment_fee or Decimal("0"))


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"${value:,.0f}"


def deal_summary(facts: DealFacts) -> str:
    beds = facts.property_facts.get("beds")
    baths = facts.property_facts.get("baths")
    sqft = facts.property_facts.get("sqft")
    bits: list[str] = []
    if beds or baths:
        bits.append(f"{beds or '?'} bd / {baths or '?'} ba")
    if sqft:
        bits.append(f"{sqft:,} sqft" if isinstance(sqft, int) else f"{sqft} sqft")
    if facts.condition_class:
        bits.append(f"condition: {facts.condition_class}")
    head = " · ".join(bits)
    price = f"Assignment price: {_money(facts.asking_price)}"
    return f"{head}\n{price}" if head else price


def evidence_summary(facts: DealFacts) -> str:
    """BB-09: evidence, uncertainty, and what is still open. Never a number without a label."""
    lines: list[str] = []
    conf = facts.arv_confidence or "unknown"
    lines.append(
        f"- Estimated ARV {_money(facts.arv)} (confidence: {conf}"
        + (f", comp tier {facts.comp_tier}" if facts.comp_tier is not None else "")
        + ")"
    )
    lines.append(f"- Estimated repairs {_money(facts.repairs)} (desk estimate, not an inspection)")
    if facts.open_flags:
        lines.append("- Open items to verify: " + ", ".join(sorted(facts.open_flags)))
    else:
        lines.append("- Open items to verify: none recorded; buyer performs own due diligence")
    lines.append("- Source: county records and our purchase agreement; figures are estimates")
    return "\n".join(lines)


def property_short(line1: str, city: str | None) -> str:
    return f"{line1}, {city}" if city else line1


class S6Disposition:
    """Buyer selection and copy for one signed contract."""

    agent_id = "S6"

    def __init__(self, sender: SenderIdentity) -> None:
        self._sender = sender

    # -- facts -----------------------------------------------------------------------------

    async def load_facts(self, session: AsyncSession, contract: Contract) -> DealFacts:
        offer = await session.get(Offer, contract.offer_id)
        lead = await session.get(SellerLead, contract.seller_lead_id)
        if offer is None or lead is None:
            raise LookupError(f"contract {contract.id}: offer or seller lead missing")
        intake = await session.get(Intake, offer.intake_id)
        market_name = "the area"
        if lead.market_id is not None:
            market = await session.get(Market, lead.market_id)
            if market is not None:
                market_name = market.name
        elif contract.property_city:
            market_name = contract.property_city
        address = contract.property_address_line1
        tail = ", ".join(p for p in (contract.property_city, contract.property_state) if p)
        if tail:
            address += f", {tail}"
        if contract.property_postal_code:
            address += f" {contract.property_postal_code}"
        return DealFacts(
            contract_id=contract.id,
            contract_signed=contract.status is ContractStatus.SIGNED,
            market=market_name,
            property_address=address,
            property_short=property_short(contract.property_address_line1, contract.property_city),
            postal_code=contract.property_postal_code,
            purchase_price=contract.purchase_price
            if contract.purchase_price is not None
            else offer.offer_price,
            assignment_fee=contract.assignment_fee
            if contract.assignment_fee is not None
            else offer.assignment_fee,
            arv=offer.arv,
            arv_confidence=offer.arv_confidence.value if offer.arv_confidence else None,
            repairs=offer.repairs,
            comp_tier=offer.comp_tier,
            condition_class=(
                intake.condition_class.value if intake and intake.condition_class else None
            ),
            open_flags=list(offer.open_flags or []),
            property_facts=dict((intake.property_facts if intake else {}) or {}),
        )

    # -- selection -------------------------------------------------------------------------

    async def select_buyers(
        self,
        session: AsyncSession,
        contract: Contract,
        *,
        facts: DealFacts | None = None,
        include_fallback: bool = True,
        limit: int | None = None,
    ) -> list[BuyerCandidate]:
        facts = facts or await self.load_facts(session, contract)
        lead = await session.get(SellerLead, contract.seller_lead_id)
        closed = await self._closed_deals_by_investor(session)
        emails = await self._primary_emails(session)

        candidates: dict[uuid.UUID, BuyerCandidate] = {}
        for inv, match in await self._approved_match_investors(session, contract, lead):
            if inv.id in candidates or inv.id not in emails:
                continue
            candidates[inv.id] = self._candidate(
                inv,
                emails[inv.id],
                tier=TIER_APPROVED_MATCH,
                closed=closed.get(inv.id, 0),
                reason=f"approved D2 match {match.id} (score {match.score})",
                match_id=match.id,
                buy_box_id=None,
            )
        if include_fallback or not candidates:
            for inv, box, why in await self._buy_box_fit_investors(session, lead, facts):
                if inv.id in candidates or inv.id not in emails:
                    continue
                candidates[inv.id] = self._candidate(
                    inv,
                    emails[inv.id],
                    tier=TIER_BUY_BOX_FIT,
                    closed=closed.get(inv.id, 0),
                    reason=why,
                    match_id=None,
                    buy_box_id=box.id,
                )
        ordered = sorted(candidates.values(), key=lambda c: c.sort_key)
        return ordered[:limit] if limit is not None else ordered

    @staticmethod
    def _candidate(
        inv: Investor,
        email: str,
        *,
        tier: int,
        closed: int,
        reason: str,
        match_id: uuid.UUID | None,
        buy_box_id: uuid.UUID | None,
    ) -> BuyerCandidate:
        return BuyerCandidate(
            investor_id=inv.id,
            name=inv.canonical_name,
            first_name=inv.first_name,
            email=email,
            tier=tier,
            closed_deals=closed,
            score=int(inv.score or 0),
            purchase_count_24m=int(inv.purchase_count_24m or 0),
            reason=reason,
            match_id=match_id,
            buy_box_id=buy_box_id,
        )

    async def _approved_match_investors(
        self, session: AsyncSession, contract: Contract, lead: SellerLead | None
    ) -> list[tuple[Investor, Match]]:
        conds = [PropertyCandidate.seller_lead_id == contract.seller_lead_id]
        if lead is not None and lead.address_key:
            conds.append(PropertyCandidate.address_key == lead.address_key)
        if lead is not None and lead.parcel_id:
            conds.append(PropertyCandidate.parcel_id == lead.parcel_id)
        from sqlalchemy import or_

        stmt = (
            select(Investor, Match)
            .join(SearchProfile, SearchProfile.id == Match.search_profile_id)
            .join(BuyBox, BuyBox.id == SearchProfile.buy_box_id)
            .join(Investor, Investor.id == BuyBox.investor_id)
            .join(PropertyCandidate, PropertyCandidate.id == Match.property_candidate_id)
            .where(Match.review_status == MatchReviewStatus.APPROVED, or_(*conds))
            .where(Investor.excluded_reason.is_(None))
            .order_by(Match.score.desc().nulls_last(), Investor.canonical_name)
        )
        return [(inv, m) for inv, m in (await session.execute(stmt)).all()]

    async def _buy_box_fit_investors(
        self, session: AsyncSession, lead: SellerLead | None, facts: DealFacts
    ) -> list[tuple[Investor, BuyBox, str]]:
        stmt = (
            select(Investor, BuyBox)
            .join(BuyBox, BuyBox.investor_id == Investor.id)
            .where(BuyBox.is_current.is_(True), BuyBox.is_active.is_(True))
            .where(Investor.excluded_reason.is_(None))
        )
        if lead is not None and lead.market_id is not None:
            stmt = stmt.where(Investor.market_id == lead.market_id)
        price = facts.asking_price
        out: list[tuple[Investor, BuyBox, str]] = []
        for inv, box in (await session.execute(stmt)).all():
            fit, why = buy_box_fits(box, price=price, postal_code=facts.postal_code)
            if fit:
                out.append((inv, box, why))
        return out

    async def _closed_deals_by_investor(self, session: AsyncSession) -> dict[uuid.UUID, int]:
        stmt = (
            select(Contract.buyer_investor_id, func.count())
            .where(
                Contract.type == ContractType.PSA,
                Contract.status == ContractStatus.CLOSED,
                Contract.buyer_investor_id.is_not(None),
            )
            .group_by(Contract.buyer_investor_id)
        )
        return {inv_id: int(n) for inv_id, n in (await session.execute(stmt)).all() if inv_id}

    async def _primary_emails(self, session: AsyncSession) -> dict[uuid.UUID, str]:
        stmt = (
            select(Contact.investor_id, Contact.value, Contact.is_primary)
            .where(Contact.channel == ContactChannel.EMAIL)
            .order_by(Contact.is_primary.desc(), Contact.created_at)
        )
        out: dict[uuid.UUID, str] = {}
        for investor_id, value, _primary in (await session.execute(stmt)).all():
            out.setdefault(investor_id, value)
        return out

    # -- copy --------------------------------------------------------------------------------

    def draft(self, facts: DealFacts, buyer: BuyerCandidate) -> DispositionCopy:
        return render_blast(
            contract_signed=facts.contract_signed,
            first_name=buyer.first_name,
            market=facts.market,
            property_address=facts.property_address,
            property_short=facts.property_short,
            deal_summary=deal_summary(facts),
            evidence_summary=evidence_summary(facts),
            sender=self._sender,
        )


def buy_box_fits(
    box: BuyBox, *, price: Decimal | None, postal_code: str | None
) -> tuple[bool, str]:
    """Price band contains ``price`` (unknown bounds pass) and the ZIP is listed or the box
    names no ZIPs. Returns ``(fits, reason)``."""
    if price is not None:
        if box.price_min is not None and price < box.price_min:
            return False, f"price {price} below buy box min {box.price_min}"
        if box.price_max is not None and price > box.price_max:
            return False, f"price {price} above buy box max {box.price_max}"
    zips = _zips(box.geography)
    if zips and (postal_code is None or postal_code[:5] not in zips):
        return False, f"ZIP {postal_code} not in buy box ZIPs"
    why = "current buy box fits: price band"
    why += f" and ZIP {postal_code[:5]}" if zips and postal_code else " (no ZIP restriction)"
    return True, why


def _zips(geography: Mapping[str, Any]) -> set[str]:
    raw = geography.get("zips") or geography.get("zip_codes") or []
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return set()
    return {str(z).strip()[:5] for z in raw if str(z).strip()}


class S6QaAgent(A5QaAgent):
    """A5 with the single, fact-backed exemption described in the module docstring."""

    def __init__(self, *args: Any, contract_signed: bool, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._contract_signed = contract_signed

    def prechecks(self, inp: QaInput) -> list[str]:
        flags = run_prechecks(inp)
        if self._contract_signed:
            flags = [f for f in flags if f != UNDER_CONTRACT_FLAG]
        return flags

    async def review(self, inp: QaInput) -> QaResult:
        flags = self.prechecks(inp)
        if flags:
            return QaResult(
                verdict=QaVerdict.BLOCK,
                confidence=1.0,
                flags=flags,
                rationale="blocked by deterministic pre-check: " + ", ".join(flags),
            )
        request = self.build_request(TaskKind.QA, self.review_prompt(inp), json_schema=QA_SCHEMA)
        response, run_id = await self.run(TaskKind.QA, request)
        parsed = response.parsed
        if parsed is None:
            raise StructuredOutputError("A5 QA returned no structured output")
        try:
            verdict = QaVerdict(str(parsed.get("verdict", "")))
        except ValueError as exc:
            raise StructuredOutputError(
                f"A5 returned unknown verdict {parsed.get('verdict')!r}"
            ) from exc
        model_flags = [str(f) for f in parsed.get("flags") or []]
        rewrite = parsed.get("rewrite_body")
        if verdict is QaVerdict.REWRITE and not rewrite:
            model_flags.append("rewrite_without_body")
        return QaResult(
            verdict=verdict,
            confidence=min(1.0, max(0.0, float(parsed.get("confidence", 0.0)))),
            flags=model_flags,
            rationale=str(parsed.get("rationale", "")),
            rewrite_body=str(rewrite) if rewrite else None,
            run_id=run_id,
            model=response.model,
        )


__all__ = [
    "TIER_APPROVED_MATCH",
    "TIER_BUY_BOX_FIT",
    "UNDER_CONTRACT_FLAG",
    "BuyerCandidate",
    "DealFacts",
    "S6Disposition",
    "S6QaAgent",
    "buy_box_fits",
    "deal_summary",
    "evidence_summary",
    "property_short",
]
