"""Step 2 - comp pull with tier expansion (REQUIREMENTS.md A7-03).

Tier 1: same ZIP, +-20% sqft, +-15 years, <=180 days, <=0.75 mi. Expand to tier 2 (1.0 mi /
270 d) and tier 3 (1.5 mi / 365 d) *only* when fewer than ``MIN_COMPS`` (3) qualify. The tier
used is recorded on the offer (``offers.comp_tier``) and in every ARV verification record.

``pull_comps`` is also what V2 (next-tier re-run) and V6 (pre-send re-pull) call, with
``start_tier`` / ``exclude_ids`` so audited-out comps (V3) stay out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from wholesaling.config.tunables import A7Config
from wholesaling.integrations.data.property import CompQuery, PropertyDataProvider, SoldComp

MIN_COMPS = 3
SQFT_TOLERANCE_PCT = Decimal("0.20")
YEAR_TOLERANCE = 15


@dataclass(frozen=True, slots=True)
class CompTier:
    tier: int
    max_miles: Decimal
    max_days: int

    def to_json(self) -> dict[str, Any]:
        return {"tier": self.tier, "max_miles": str(self.max_miles), "max_days": self.max_days}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CompTier:
        return cls(
            tier=int(d["tier"]), max_miles=Decimal(str(d["max_miles"])), max_days=int(d["max_days"])
        )


def tiers_from_config(config: A7Config) -> list[CompTier]:
    tiers = [
        CompTier(
            tier=int(row["tier"]),
            max_miles=Decimal(str(row["max_miles"])),
            max_days=int(row["max_days"]),
        )
        for row in config.comp_tiers
    ]
    tiers.sort(key=lambda t: t.tier)
    if not tiers:
        msg = "config/a7.yaml comp_tiers must list at least one tier"
        raise ValueError(msg)
    return tiers


def tier_by_number(tiers: list[CompTier], number: int) -> CompTier:
    for t in tiers:
        if t.tier == number:
            return t
    msg = f"no comp tier {number}"
    raise KeyError(msg)


@dataclass(slots=True)
class CompPull:
    """What step 2 produced: the comps at the tier finally used plus the search trail."""

    tier: CompTier
    comps: list[SoldComp]
    attempts: list[dict[str, Any]] = field(default_factory=list)  # per tier tried
    excluded_ids: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.comps)

    def to_json(self) -> dict[str, Any]:
        return {
            "tier": self.tier.to_json(),
            "count": self.count,
            "comp_ids": [c.comp_id for c in self.comps],
            "attempts": list(self.attempts),
            "excluded_ids": list(self.excluded_ids),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any], comps: list[SoldComp]) -> CompPull:
        return cls(
            tier=CompTier.from_json(d["tier"]),
            comps=list(comps),
            attempts=[dict(a) for a in d.get("attempts") or []],
            excluded_ids=[str(x) for x in d.get("excluded_ids") or []],
        )


def build_query(
    *,
    postal_code: str,
    parcel_id: str | None,
    subject_sqft: int,
    subject_year_built: int | None,
    tier: CompTier,
    as_of: date,
) -> CompQuery:
    return CompQuery(
        postal_code=postal_code,
        parcel_id=parcel_id,
        subject_sqft=subject_sqft,
        subject_year_built=subject_year_built,
        max_miles=tier.max_miles,
        max_days=tier.max_days,
        as_of=as_of,
        sqft_tolerance_pct=SQFT_TOLERANCE_PCT,
        year_tolerance=YEAR_TOLERANCE,
    )


async def pull_comps(
    provider: PropertyDataProvider,
    *,
    postal_code: str,
    parcel_id: str | None,
    subject_sqft: int,
    subject_year_built: int | None,
    tiers: list[CompTier],
    as_of: date,
    start_tier: int = 1,
    exclude_ids: Iterable[str] = (),
    min_comps: int = MIN_COMPS,
) -> CompPull:
    """Pull at ``start_tier``; expand one tier at a time only while fewer than ``min_comps``
    qualify. The last tier is used even when it is still short (the ARV step then reports Low
    confidence, A7-04)."""
    excluded = {str(x) for x in exclude_ids}
    attempts: list[dict[str, Any]] = []
    chosen: CompTier | None = None
    found: list[SoldComp] = []
    for tier in tiers:
        if tier.tier < start_tier:
            continue
        query = build_query(
            postal_code=postal_code,
            parcel_id=parcel_id,
            subject_sqft=subject_sqft,
            subject_year_built=subject_year_built,
            tier=tier,
            as_of=as_of,
        )
        raw = await provider.sold_comps(query)
        found = [c for c in raw if c.comp_id not in excluded]
        attempts.append(
            {
                "tier": tier.tier,
                "returned": len(raw),
                "after_exclusions": len(found),
                "query": query.to_json(),
            }
        )
        chosen = tier
        if len(found) >= min_comps:
            break
    if chosen is None:
        msg = f"no comp tier >= {start_tier} configured"
        raise ValueError(msg)
    return CompPull(tier=chosen, comps=found, attempts=attempts, excluded_ids=sorted(excluded))


__all__ = [
    "MIN_COMPS",
    "SQFT_TOLERANCE_PCT",
    "YEAR_TOLERANCE",
    "CompPull",
    "CompTier",
    "build_query",
    "pull_comps",
    "tier_by_number",
    "tiers_from_config",
]
