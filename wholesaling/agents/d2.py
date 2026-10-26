"""D2 - property matcher (REQUIREMENTS.md BB-07..BB-10, BB-12).

For one search-profile version, D2 takes candidates from the approved sources (the property
provider's active listings, plus off-market candidates from A7 offers with status ``accepted``),
dedupes them by parcel id / address key, estimates each one with A7's own arithmetic (ARV via
``a7.arv``, repairs via ``a7.repairs``, MAO/fees via ``a7.mao``) and ranks the in-profile ones by
buy-box fit and confidence. Every match carries the BB-08 evidence block: source, retrieval time,
match logic, missing fields, source restrictions, freshness, valuation uncertainty, the next
review action and the reason for the match, with ``review_status=pending`` (BB-09).

D2 never states that a seller has agreed to sell: every evidence text passes
``templates.check_no_agreement_claim`` and off-market candidates are described by their offer
status with the human-signature step still ahead (SEL-11).

Hard filters (candidate excluded, no match row): outside the profile's geography; property class
not in the profile; list price more than ``PRICE_SLACK`` (25%) outside the range; condition worse
than the tolerance when both are known. Soft fit is scored 0..1 by weighted components.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from wholesaling.agents.a7.arv import ArvEstimate, compute_arv
from wholesaling.agents.a7.comps import CompTier, pull_comps
from wholesaling.agents.a7.mao import MaoInputs, MaoResult, compute_mao
from wholesaling.agents.a7.repairs import estimate_repairs
from wholesaling.agents.a7.templates import check_no_agreement_claim
from wholesaling.agents.d1 import (
    CLASS_COMMERCIAL,
    CLASS_CONDO_TOWNHOME,
    CLASS_LAND,
    CLASS_SFR,
    CLASS_SMALL_MULTI,
    condition_within,
    normalize_city,
    normalize_market,
)
from wholesaling.config.tunables import A7Config
from wholesaling.db.models.enums import ArvConfidence, ConditionClass
from wholesaling.integrations.data.property import ActiveListing, PropertyDataProvider

PRICE_SLACK = Decimal("0.25")
WEIGHTS: dict[str, Decimal] = {
    "geography": Decimal("0.20"),
    "property_class": Decimal("0.15"),
    "price_fit": Decimal("0.25"),
    "condition": Decimal("0.15"),
    "spread": Decimal("0.25"),
}
CONFIDENCE_FACTOR: dict[ArvConfidence, Decimal] = {
    ArvConfidence.HIGH: Decimal("1.0"),
    ArvConfidence.MEDIUM: Decimal("0.75"),
    ArvConfidence.LOW: Decimal("0.40"),
}
SOURCE_RESTRICTIONS = (
    "mock provider in this build; before live use confirm the provider licence permits "
    "automated use and redistribution (INT-02)"
)
SOURCE_A7_CONTRACT = "a7_contract"
SELLER_INTENT_LISTING = "unknown: no contact made; listing status only"
SELLER_INTENT_OFF_MARKET = (
    "A7 offer status accepted; purchase contract still requires a human signature (SEL-11); "
    "not available to buyers until a human approves the deal"
)
NEXT_REVIEW_ACTION = (
    "human review: verify title and liens, HOA, occupancy and seller intent before this is "
    "described to any buyer"
)

_Q4 = Decimal("0.0001")
_CLASS_FOR_TYPE: dict[str, str] = {
    "sfr": CLASS_SFR,
    "duplex": CLASS_SMALL_MULTI,
    "triplex": CLASS_SMALL_MULTI,
    "fourplex": CLASS_SMALL_MULTI,
    "condo": CLASS_CONDO_TOWNHOME,
    "townhouse": CLASS_CONDO_TOWNHOME,
    "townhome": CLASS_CONDO_TOWNHOME,
    "land": CLASS_LAND,
    "commercial": CLASS_COMMERCIAL,
}
_CONDITION_CLASS: dict[str, ConditionClass] = {
    "light": ConditionClass.LIGHT,
    "medium": ConditionClass.MEDIUM,
    "heavy": ConditionClass.HEAVY,
    "gut": ConditionClass.GUT,
}


def property_class_of(listing: ActiveListing) -> str:
    return _CLASS_FOR_TYPE.get(listing.property_type.lower(), listing.property_type.lower())


@dataclass(slots=True)
class CandidateEstimate:
    """One deduped candidate with its separate estimates (BB-07)."""

    listing: ActiveListing
    arv: ArvEstimate | None
    repairs_total: Decimal | None
    condition_assumed: ConditionClass
    mao: MaoResult | None
    fees: Decimal
    assignment_spread: Decimal | None  # MAO - list price (what is left for an assignment)
    liquidity_score: Decimal  # tier-1 comps / 10, capped at 1
    comp_count: int
    comp_tier: int | None
    flags: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    seller_lead_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None

    @property
    def is_off_market(self) -> bool:
        return self.listing.source == SOURCE_A7_CONTRACT

    def facts(self) -> dict[str, Any]:
        li = self.listing
        return {
            "beds": li.beds,
            "baths": str(li.baths) if li.baths is not None else None,
            "sqft": li.sqft,
            "year_built": li.year_built,
            "lot_sqft": li.lot_sqft,
            "property_type": li.property_type,
            "units": li.units,
            "condition_hint": li.condition_hint,
            "condition_assumed": self.condition_assumed.value,
            "arv": self.arv.to_json() if self.arv else None,
            "comp_tier": self.comp_tier,
            "comp_count": self.comp_count,
            "mao": self.mao.to_json() if self.mao else None,
            "list_price": str(li.list_price) if li.list_price is not None else None,
        }


@dataclass(slots=True)
class MatchResult:
    estimate: CandidateEstimate
    score: Decimal
    confidence: Decimal
    components: dict[str, Decimal]
    reason: str
    evidence: dict[str, Any]


def _flags_for(listing: ActiveListing) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    missing: list[str] = []
    if listing.hoa:
        flags.append("hoa")
    if listing.title_notes:
        flags.append("title_liens")
    if listing.financing_terms:
        flags.append(f"financing_constraint:{listing.financing_terms.strip().lower()}")
    if listing.units > 1:
        flags.append("multi_unit")
    if property_class_of(listing) in (CLASS_LAND, CLASS_COMMERCIAL):
        flags.append("non_residential")
    for name in ("sqft", "year_built", "list_price", "condition_hint", "beds", "baths"):
        if getattr(listing, name) is None:
            missing.append(name)
            flags.append(f"data_gap:{name}")
    if listing.parcel_id is None:
        missing.append("parcel_id")
        flags.append("data_gap:parcel_id")
    return flags, missing


async def estimate_candidate(
    listing: ActiveListing,
    *,
    provider: PropertyDataProvider,
    config: A7Config,
    tiers: list[CompTier],
    as_of: date,
    market_name: str | None,
    seller_lead_id: uuid.UUID | None = None,
    offer_id: uuid.UUID | None = None,
    known_arv: Decimal | None = None,
    known_repairs: Decimal | None = None,
) -> CandidateEstimate:
    """Separate ARV / repairs / fees / spread / liquidity estimates for one candidate.

    Off-market candidates arrive with A7's verified ARV and repairs (``known_*``), which win
    over a fresh estimate; listings are estimated from scratch with the same functions."""
    flags, missing = _flags_for(listing)
    fee = Decimal(str(config.assignment_fee))
    condition = _CONDITION_CLASS.get((listing.condition_hint or "").lower(), ConditionClass.MEDIUM)
    arv: ArvEstimate | None = None
    comp_count = 0
    comp_tier: int | None = None
    liquidity = Decimal(0)
    if listing.sqft and listing.address.postal_code:
        pull = await pull_comps(
            provider,
            postal_code=listing.address.postal_code,
            parcel_id=listing.parcel_id,
            subject_sqft=listing.sqft,
            subject_year_built=listing.year_built,
            tiers=tiers,
            as_of=as_of,
        )
        comp_count = pull.count
        comp_tier = pull.tier.tier
        tier1 = next((a for a in pull.attempts if a["tier"] == 1), None)
        tier1_count = int(tier1["after_exclusions"]) if tier1 else 0
        liquidity = min(Decimal(1), Decimal(tier1_count) / Decimal(10)).quantize(_Q4)
        if pull.comps:
            arv = compute_arv(pull.comps, subject_sqft=listing.sqft, tier=pull.tier, as_of=as_of)
    arv_value = known_arv if known_arv is not None else (arv.arv if arv else None)
    repairs_total: Decimal | None = known_repairs
    if repairs_total is None and listing.sqft:
        repairs_total = estimate_repairs(
            config,
            sqft=listing.sqft,
            condition_class=condition,
            disclosed_issues=[],
            market_name=market_name,
            city=listing.address.city,
        ).total
    mao: MaoResult | None = None
    spread: Decimal | None = None
    if arv_value is not None and repairs_total is not None:
        mao = compute_mao(MaoInputs.from_config(config, arv=arv_value, repairs=repairs_total))
        if listing.list_price is not None:
            spread = (mao.mao - listing.list_price).quantize(Decimal("0.01"))
    if arv_value is None:
        flags.append("no_arv_estimate")
    return CandidateEstimate(
        listing=listing,
        arv=arv,
        repairs_total=repairs_total,
        condition_assumed=condition,
        mao=mao,
        fees=fee,
        assignment_spread=spread,
        liquidity_score=liquidity,
        comp_count=comp_count,
        comp_tier=comp_tier,
        flags=flags,
        missing_fields=missing,
        seller_lead_id=seller_lead_id,
        offer_id=offer_id,
    )


def _geography_fit(criteria: dict[str, Any], listing: ActiveListing) -> Decimal | None:
    geo = criteria.get("geography") or {}
    zips = set(geo.get("zips") or [])
    cities = set(geo.get("cities") or [])
    markets = set(geo.get("markets") or [])
    neighborhoods = set(geo.get("neighborhoods") or [])
    if not (zips or cities or markets or neighborhoods):
        return Decimal("0.5")  # geography unknown: neither in nor out
    if listing.address.postal_code and listing.address.postal_code in zips:
        return Decimal(1)
    city = normalize_city(listing.address.city or "") if listing.address.city else ""
    if city and city in cities:
        return Decimal("0.8")
    market = normalize_market(listing.market) if listing.market else ""
    if market and market in markets:
        return Decimal("0.6")
    if city and any(normalize_market(m).startswith(city) for m in markets):
        return Decimal("0.6")
    return None


def _price_fit(criteria: dict[str, Any], est: CandidateEstimate) -> Decimal | None:
    """1.0 inside the range; linear to 0 at ``PRICE_SLACK`` outside; ``None`` beyond (exclude)."""
    price = est.listing.list_price
    if price is None:
        return Decimal("0.5")
    lo = Decimal(criteria["price_min"]) if criteria.get("price_min") else None
    hi = Decimal(criteria["price_max"]) if criteria.get("price_max") else None
    if lo is None and hi is None:
        return Decimal("0.5")
    over = Decimal(0)
    if hi is not None and price > hi:
        over = (price - hi) / hi
    elif lo is not None and price < lo:
        over = (lo - price) / lo
    if over == 0:
        return Decimal(1)
    if over > PRICE_SLACK:
        return None
    return (Decimal(1) - over / PRICE_SLACK).quantize(_Q4, rounding=ROUND_HALF_UP)


def _spread_fit(est: CandidateEstimate) -> Decimal:
    if est.assignment_spread is None:
        return Decimal("0.3")
    if est.assignment_spread <= 0:
        return Decimal(0)
    return min(Decimal(1), est.assignment_spread / est.fees).quantize(_Q4, rounding=ROUND_HALF_UP)


class D2PropertyMatcher:
    def __init__(self, *, as_of: date) -> None:
        self.as_of = as_of

    @staticmethod
    def dedupe(candidates: list[ActiveListing]) -> tuple[list[ActiveListing], list[dict[str, str]]]:
        """Keep the first candidate per parcel id / address key; report what was collapsed."""
        kept: list[ActiveListing] = []
        seen_parcels: dict[str, str] = {}
        seen_addresses: dict[str, str] = {}
        dropped: list[dict[str, str]] = []
        for c in candidates:
            key = c.address.key
            dup_of = None
            if c.parcel_id and c.parcel_id in seen_parcels:
                dup_of = seen_parcels[c.parcel_id]
            elif key in seen_addresses:
                dup_of = seen_addresses[key]
            if dup_of is not None:
                dropped.append(
                    {"listing_id": c.listing_id, "source": c.source, "duplicate_of": dup_of}
                )
                continue
            if c.parcel_id:
                seen_parcels[c.parcel_id] = c.listing_id
            seen_addresses[key] = c.listing_id
            kept.append(c)
        return kept, dropped

    def score(self, criteria: dict[str, Any], est: CandidateEstimate) -> MatchResult | None:
        """``None`` when a hard filter excludes the candidate; otherwise the ranked match."""
        listing = est.listing
        geo = _geography_fit(criteria, listing)
        if geo is None:
            return None
        classes = list(criteria.get("property_classes") or [])
        cls = property_class_of(listing)
        if classes and cls not in classes:
            return None
        price = _price_fit(criteria, est)
        if price is None:
            return None
        within = condition_within(criteria.get("condition_tolerance"), listing.condition_hint)
        if within is False:
            return None
        components = {
            "geography": geo,
            "property_class": Decimal(1) if classes else Decimal("0.5"),
            "price_fit": price,
            "condition": Decimal(1) if within else Decimal("0.5"),
            "spread": _spread_fit(est),
        }
        score = sum((WEIGHTS[k] * v for k, v in components.items()), Decimal(0)).quantize(
            _Q4, rounding=ROUND_HALF_UP
        )
        completeness = Decimal(1) - Decimal(len(est.missing_fields)) / Decimal(10)
        arv_conf = est.arv.confidence if est.arv else ArvConfidence.LOW
        confidence = (max(Decimal(0), completeness) * CONFIDENCE_FACTOR[arv_conf]).quantize(
            _Q4, rounding=ROUND_HALF_UP
        )
        reason = self._reason(criteria, est, components)
        evidence = self._evidence(criteria, est, components, confidence, reason)
        return MatchResult(
            estimate=est,
            score=score,
            confidence=confidence,
            components=components,
            reason=reason,
            evidence=evidence,
        )

    def _reason(
        self, criteria: dict[str, Any], est: CandidateEstimate, components: dict[str, Decimal]
    ) -> str:
        li = est.listing
        bits = [
            f"{property_class_of(li)} in "
            f"{li.address.postal_code or li.address.city or 'profile area'}"
        ]
        if li.list_price is not None:
            bits.append(f"listed at ${int(li.list_price):,}")
        if est.mao is not None:
            bits.append(f"MAO ${int(est.mao.mao):,}")
        if est.assignment_spread is not None:
            bits.append(f"spread ${int(est.assignment_spread):,} vs ${int(est.fees):,} fee target")
        if est.is_off_market:
            bits.append("off-market from A7")
        text = (
            "; ".join(bits)
            + f"; fit {components['price_fit']:.2f} price / {components['geography']:.2f} geography"
        )
        if check_no_agreement_claim(text):  # pragma: no cover - template guard
            msg = "D2 reason text would imply seller agreement"
            raise ValueError(msg)
        return text

    def _evidence(
        self,
        criteria: dict[str, Any],
        est: CandidateEstimate,
        components: dict[str, Decimal],
        confidence: Decimal,
        reason: str,
    ) -> dict[str, Any]:
        li = est.listing
        retrieved = li.retrieved_at or self.as_of
        freshness_days = (self.as_of - retrieved).days
        uncertainty = {
            "arv_confidence": est.arv.confidence.value if est.arv else "none",
            "arv_spread_pct": str(est.arv.spread_pct) if est.arv else None,
            "comp_count": est.comp_count,
            "comp_tier": est.comp_tier,
            "repairs_assumed_class": est.condition_assumed.value,
            "repairs_basis": "listing condition hint"
            if li.condition_hint
            else "default Medium (unknown)",
        }
        evidence = {
            "source": li.source,
            "source_url": li.source_url,
            "external_id": li.listing_id,
            "retrieved_at": retrieved.isoformat(),
            "freshness": {
                "days_since_retrieval": freshness_days,
                "days_on_market": li.days_on_market,
            },
            "match_logic": {
                "weights": {k: str(v) for k, v in WEIGHTS.items()},
                "components": {k: str(v) for k, v in components.items()},
                "profile_criteria": {
                    k: v
                    for k, v in criteria.items()
                    if k
                    in (
                        "geography",
                        "property_classes",
                        "price_min",
                        "price_max",
                        "condition_tolerance",
                    )
                },
            },
            "estimates": {
                "arv": str(est.arv.arv) if est.arv else None,
                "repairs": str(est.repairs_total) if est.repairs_total is not None else None,
                "fees": str(est.fees),
                "mao": str(est.mao.mao) if est.mao else None,
                "assignment_spread": str(est.assignment_spread)
                if est.assignment_spread is not None
                else None,
                "liquidity_score": str(est.liquidity_score),
            },
            "missing_fields": list(est.missing_fields),
            "flags": list(est.flags),
            "source_restrictions": SOURCE_RESTRICTIONS,
            "valuation_uncertainty": uncertainty,
            "confidence": str(confidence),
            "seller_intent": SELLER_INTENT_OFF_MARKET
            if est.is_off_market
            else SELLER_INTENT_LISTING,
            "reason": reason,
            "next_review_action": NEXT_REVIEW_ACTION,
            "review_status": "pending",
        }
        for text in (reason, evidence["seller_intent"], NEXT_REVIEW_ACTION):
            if check_no_agreement_claim(str(text)):  # pragma: no cover - template guard
                msg = "D2 evidence text would imply seller agreement"
                raise ValueError(msg)
        return evidence

    def rank(
        self, criteria: dict[str, Any], estimates: list[CandidateEstimate]
    ) -> list[MatchResult]:
        matches = [m for m in (self.score(criteria, e) for e in estimates) if m is not None]
        matches.sort(key=lambda m: (m.score, m.confidence), reverse=True)
        return matches


__all__ = [
    "CONFIDENCE_FACTOR",
    "NEXT_REVIEW_ACTION",
    "PRICE_SLACK",
    "SELLER_INTENT_LISTING",
    "SELLER_INTENT_OFF_MARKET",
    "SOURCE_A7_CONTRACT",
    "SOURCE_RESTRICTIONS",
    "WEIGHTS",
    "CandidateEstimate",
    "D2PropertyMatcher",
    "MatchResult",
    "estimate_candidate",
    "property_class_of",
]
