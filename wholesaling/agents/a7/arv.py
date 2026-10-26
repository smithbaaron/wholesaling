"""Step 3 - ARV from comps (REQUIREMENTS.md A7-04, ENG-Q10).

Method, in order:

1. price per sqft for every comp (``sale_price / sqft``, 4 dp);
2. trim the top and bottom 15% by price/sqft: ``k = floor(n * 0.15)`` comps off each end,
   reduced so at least 3 remain (with 7 comps one comes off each end; with 3..6 none);
3. weight each kept comp by ``recency_weight * distance_weight``, each a linear decay from 1.0 at
   0 days / 0 mi to 0.5 at the tier's day / mile limit (ENG-Q10);
4. ARV = weighted mean price/sqft x subject sqft, rounded to the dollar.

Spread = ``(max - min) / weighted mean`` of the kept comps' price/sqft. Confidence (A7-04):
High when >=5 comps, tier 1 and spread <=12%; Medium when >=3 comps, tier <=2 and spread <=20%;
Low otherwise (Low means a human decides, A7-08 gate 1).

Pure functions; the engine calls them after every comp set change (V3 removals, V2 and V6
re-pulls) so each verification record carries the ARV that resulted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from wholesaling.agents.a7.comps import CompTier
from wholesaling.db.models.enums import ArvConfidence
from wholesaling.integrations.data.property import SoldComp

TRIM_PCT = Decimal("0.15")
MIN_KEPT = 3
DECAY_FLOOR = Decimal("0.5")  # weight at the tier limit (ENG-Q10)
HIGH_MIN_COMPS = 5
HIGH_MAX_TIER = 1
HIGH_MAX_SPREAD = Decimal("0.12")
MEDIUM_MIN_COMPS = 3
MEDIUM_MAX_TIER = 2
MEDIUM_MAX_SPREAD = Decimal("0.20")

_Q4 = Decimal("0.0001")
_Q6 = Decimal("0.000001")
_DOLLAR = Decimal("1")

CONFIDENCE_ORDER: tuple[ArvConfidence, ...] = (
    ArvConfidence.LOW,
    ArvConfidence.MEDIUM,
    ArvConfidence.HIGH,
)


@dataclass(frozen=True, slots=True)
class WeightedComp:
    comp_id: str
    price_per_sqft: Decimal
    days_old: int
    distance_miles: Decimal
    recency_weight: Decimal
    distance_weight: Decimal
    weight: Decimal
    trimmed: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "comp_id": self.comp_id,
            "price_per_sqft": str(self.price_per_sqft),
            "days_old": self.days_old,
            "distance_miles": str(self.distance_miles),
            "recency_weight": str(self.recency_weight),
            "distance_weight": str(self.distance_weight),
            "weight": str(self.weight),
            "trimmed": self.trimmed,
        }


@dataclass(frozen=True, slots=True)
class ArvEstimate:
    arv: Decimal
    weighted_price_per_sqft: Decimal
    subject_sqft: int
    tier: int
    comp_count: int  # comps that entered the calculation (before trimming)
    kept_count: int
    spread_pct: Decimal
    confidence: ArvConfidence
    confidence_reason: str
    comps: list[WeightedComp] = field(default_factory=list)

    @property
    def kept_ids(self) -> list[str]:
        return [c.comp_id for c in self.comps if not c.trimmed]

    @property
    def trimmed_ids(self) -> list[str]:
        return [c.comp_id for c in self.comps if c.trimmed]

    def to_json(self) -> dict[str, Any]:
        return {
            "arv": str(self.arv),
            "weighted_price_per_sqft": str(self.weighted_price_per_sqft),
            "subject_sqft": self.subject_sqft,
            "tier": self.tier,
            "comp_count": self.comp_count,
            "kept_count": self.kept_count,
            "spread_pct": str(self.spread_pct),
            "confidence": self.confidence.value,
            "confidence_reason": self.confidence_reason,
            "comps": [c.to_json() for c in self.comps],
            "method": (
                "trim 15% each end by price/sqft; weight = recency x distance, linear 1.0 -> 0.5 "
                "at tier limits; weighted mean x subject sqft (ENG-Q10)"
            ),
        }


def trim_count(n: int, trim_pct: Decimal = TRIM_PCT, min_kept: int = MIN_KEPT) -> int:
    """How many comps come off *each* end: floor(n x 15%), reduced so >= ``min_kept`` remain."""
    if n <= min_kept:
        return 0
    k = int((Decimal(n) * trim_pct).to_integral_value(rounding="ROUND_FLOOR"))
    while k > 0 and n - 2 * k < min_kept:
        k -= 1
    return k


def linear_decay(value: Decimal, limit: Decimal) -> Decimal:
    """1.0 at 0, ``DECAY_FLOOR`` at ``limit`` (ENG-Q10). Clamped to the floor beyond the limit."""
    if limit <= 0:
        return Decimal(1)
    ratio = min(Decimal(1), max(Decimal(0), value / limit))
    return (Decimal(1) - (Decimal(1) - DECAY_FLOOR) * ratio).quantize(_Q6, rounding=ROUND_HALF_UP)


def confidence_for(comp_count: int, tier: int, spread_pct: Decimal) -> tuple[ArvConfidence, str]:
    if comp_count >= HIGH_MIN_COMPS and tier <= HIGH_MAX_TIER and spread_pct <= HIGH_MAX_SPREAD:
        return ArvConfidence.HIGH, f"{comp_count} comps, tier {tier}, spread {spread_pct:.1%}"
    if (
        comp_count >= MEDIUM_MIN_COMPS
        and tier <= MEDIUM_MAX_TIER
        and spread_pct <= MEDIUM_MAX_SPREAD
    ):
        return ArvConfidence.MEDIUM, f"{comp_count} comps, tier {tier}, spread {spread_pct:.1%}"
    return ArvConfidence.LOW, f"{comp_count} comps, tier {tier}, spread {spread_pct:.1%}"


def lower_confidence(confidence: ArvConfidence) -> ArvConfidence:
    """One level down (High -> Medium -> Low -> Low)."""
    idx = CONFIDENCE_ORDER.index(confidence)
    return CONFIDENCE_ORDER[max(0, idx - 1)]


def compute_arv(
    comps: list[SoldComp], *, subject_sqft: int, tier: CompTier, as_of: date
) -> ArvEstimate:
    """The ENG-Q10 estimate for one comp set. Raises ``ValueError`` on an empty set."""
    if not comps:
        msg = "cannot compute ARV without comps"
        raise ValueError(msg)
    if subject_sqft <= 0:
        msg = "subject sqft must be positive"
        raise ValueError(msg)
    ordered = sorted(comps, key=lambda c: (c.price_per_sqft, c.comp_id))
    n = len(ordered)
    k = trim_count(n)
    kept_slice = ordered[k : n - k]
    kept_ids = {c.comp_id for c in kept_slice}

    weighted: list[WeightedComp] = []
    for c in ordered:
        days_old = max(0, (as_of - c.sale_date).days)
        rw = linear_decay(Decimal(days_old), Decimal(tier.max_days))
        dw = linear_decay(c.distance_miles, tier.max_miles)
        weighted.append(
            WeightedComp(
                comp_id=c.comp_id,
                price_per_sqft=c.price_per_sqft,
                days_old=days_old,
                distance_miles=c.distance_miles,
                recency_weight=rw,
                distance_weight=dw,
                weight=(rw * dw).quantize(_Q6, rounding=ROUND_HALF_UP),
                trimmed=c.comp_id not in kept_ids,
            )
        )
    kept = [w for w in weighted if not w.trimmed]
    total_weight = sum((w.weight for w in kept), Decimal(0))
    weighted_sum = sum((w.weight * w.price_per_sqft for w in kept), Decimal(0))
    mean_ppsf = (weighted_sum / total_weight).quantize(_Q4, rounding=ROUND_HALF_UP)
    arv = (mean_ppsf * Decimal(subject_sqft)).quantize(_DOLLAR, rounding=ROUND_HALF_UP)
    lo = min(w.price_per_sqft for w in kept)
    hi = max(w.price_per_sqft for w in kept)
    spread = ((hi - lo) / mean_ppsf).quantize(_Q4, rounding=ROUND_HALF_UP)
    confidence, reason = confidence_for(n, tier.tier, spread)
    return ArvEstimate(
        arv=arv,
        weighted_price_per_sqft=mean_ppsf,
        subject_sqft=subject_sqft,
        tier=tier.tier,
        comp_count=n,
        kept_count=len(kept),
        spread_pct=spread,
        confidence=confidence,
        confidence_reason=reason,
        comps=weighted,
    )


def pct_gap(a: Decimal, b: Decimal) -> Decimal:
    """|a - b| relative to the larger magnitude, 4 dp. Zero when both are zero."""
    base = max(abs(a), abs(b))
    if base == 0:
        return Decimal(0)
    return (abs(a - b) / base).quantize(_Q4, rounding=ROUND_HALF_UP)


__all__ = [
    "CONFIDENCE_ORDER",
    "DECAY_FLOOR",
    "HIGH_MAX_SPREAD",
    "HIGH_MIN_COMPS",
    "MEDIUM_MAX_SPREAD",
    "MEDIUM_MIN_COMPS",
    "MIN_KEPT",
    "TRIM_PCT",
    "ArvEstimate",
    "WeightedComp",
    "compute_arv",
    "confidence_for",
    "linear_decay",
    "lower_confidence",
    "pct_gap",
    "trim_count",
]
