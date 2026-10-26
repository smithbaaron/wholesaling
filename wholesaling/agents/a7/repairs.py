"""Step 4 - repair estimate (REQUIREMENTS.md A7-05, OPN-12).

``repairs = rate[condition_class] x sqft + disclosed line items (+ Houston foundation
contingency)``. Rates come from ``config/a7.yaml`` (``repair_rate_per_sqft``), with a per-market
override hook (``rate_override``) for the ``markets`` table once M1 populates one.

Line items: the spec names roof, foundation and HVAC as disclosed items but gives no amounts, so
the defaults below are placeholders flagged UNVALIDATED (same status as OPN-12). They belong in
``config/a7.yaml`` once validated; this module reads them from the config object when present
(``line_item_defaults`` attribute) and otherwise uses these.

Foundation: in Houston the fixed contingency from config is added whenever foundation is
disclosed *or inferred* (V4), instead of the generic foundation line item. Elsewhere the generic
line item applies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from wholesaling.config.tunables import A7Config
from wholesaling.db.models.enums import ConditionClass

# UNVALIDATED placeholders (see module docstring). Keys are normalised issue names.
LINE_ITEM_DEFAULTS: dict[str, Decimal] = {
    "roof": Decimal("12000"),
    "hvac": Decimal("8000"),
    "foundation": Decimal("10000"),
}

CLASS_ORDER: tuple[ConditionClass, ...] = (
    ConditionClass.LIGHT,
    ConditionClass.MEDIUM,
    ConditionClass.HEAVY,
    ConditionClass.GUT,
)

_ISSUE_ALIASES: dict[str, str] = {
    "roof": "roof",
    "roof leak": "roof",
    "roof damage": "roof",
    "shingles": "roof",
    "hvac": "hvac",
    "ac": "hvac",
    "a/c": "hvac",
    "air conditioning": "hvac",
    "furnace": "hvac",
    "heating": "hvac",
    "foundation": "foundation",
    "foundation issues": "foundation",
    "foundation cracks": "foundation",
    "slab": "foundation",
    "piers": "foundation",
}


def normalize_issue(raw: str) -> str | None:
    """Map a disclosed issue to ``roof`` / ``hvac`` / ``foundation``; ``None`` for anything
    else (recorded but not priced as a line item)."""
    text = " ".join(raw.strip().lower().split())
    if text in _ISSUE_ALIASES:
        return _ISSUE_ALIASES[text]
    for alias, canonical in _ISSUE_ALIASES.items():
        if alias in text:
            return canonical
    return None


def is_houston(market_name: str | None, city: str | None = None) -> bool:
    return any(value is not None and "houston" in value.lower() for value in (market_name, city))


def bump_class(current: ConditionClass) -> ConditionClass:
    """One level worse (Light -> Medium -> Heavy -> Gut -> Gut)."""
    idx = CLASS_ORDER.index(current)
    return CLASS_ORDER[min(len(CLASS_ORDER) - 1, idx + 1)]


def class_rank(c: ConditionClass) -> int:
    return CLASS_ORDER.index(c)


@dataclass(frozen=True, slots=True)
class RepairEstimate:
    condition_class: ConditionClass
    rate_per_sqft: Decimal
    sqft: int
    base: Decimal
    line_items: dict[str, Decimal]
    foundation_contingency: Decimal
    total: Decimal
    houston: bool
    unpriced_issues: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "condition_class": self.condition_class.value,
            "rate_per_sqft": str(self.rate_per_sqft),
            "sqft": self.sqft,
            "base": str(self.base),
            "line_items": {k: str(v) for k, v in self.line_items.items()},
            "foundation_contingency": str(self.foundation_contingency),
            "total": str(self.total),
            "houston": self.houston,
            "unpriced_issues": list(self.unpriced_issues),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RepairEstimate:
        return cls(
            condition_class=ConditionClass(str(d["condition_class"])),
            rate_per_sqft=Decimal(str(d["rate_per_sqft"])),
            sqft=int(d["sqft"]),
            base=Decimal(str(d["base"])),
            line_items={k: Decimal(str(v)) for k, v in (d.get("line_items") or {}).items()},
            foundation_contingency=Decimal(str(d.get("foundation_contingency", "0"))),
            total=Decimal(str(d["total"])),
            houston=bool(d.get("houston", False)),
            unpriced_issues=[str(x) for x in d.get("unpriced_issues") or []],
        )


def rate_for(config: A7Config, condition_class: ConditionClass) -> Decimal:
    key = condition_class.value.lower()
    try:
        return Decimal(str(config.repair_rate_per_sqft[key]))
    except KeyError as exc:
        msg = f"config/a7.yaml repair_rate_per_sqft has no rate for {key!r}"
        raise KeyError(msg) from exc


def estimate_repairs(
    config: A7Config,
    *,
    sqft: int,
    condition_class: ConditionClass,
    disclosed_issues: Iterable[str],
    market_name: str | None,
    city: str | None = None,
    foundation_inferred: bool = False,
    rate_override: Decimal | None = None,
    line_item_amounts: dict[str, Decimal] | None = None,
) -> RepairEstimate:
    if sqft <= 0:
        msg = "sqft must be positive to estimate repairs"
        raise ValueError(msg)
    rate = rate_override if rate_override is not None else rate_for(config, condition_class)
    amounts = {**LINE_ITEM_DEFAULTS, **(line_item_amounts or {})}
    houston = is_houston(market_name, city)
    base = (rate * Decimal(sqft)).quantize(Decimal("0.01"))

    items: dict[str, Decimal] = {}
    unpriced: list[str] = []
    foundation_flagged = foundation_inferred
    for raw in disclosed_issues:
        if not raw or not raw.strip():
            continue
        key = normalize_issue(raw)
        if key is None:
            unpriced.append(raw.strip())
            continue
        if key == "foundation":
            foundation_flagged = True
            continue
        items[key] = amounts[key]

    contingency = Decimal(0)
    if foundation_flagged:
        if houston:
            contingency = Decimal(str(config.houston_foundation_contingency))
        else:
            items["foundation"] = amounts["foundation"]

    total = base + sum(items.values(), Decimal(0)) + contingency
    return RepairEstimate(
        condition_class=condition_class,
        rate_per_sqft=rate,
        sqft=sqft,
        base=base,
        line_items=dict(sorted(items.items())),
        foundation_contingency=contingency,
        total=total.quantize(Decimal("0.01")),
        houston=houston,
        unpriced_issues=unpriced,
    )


__all__ = [
    "CLASS_ORDER",
    "LINE_ITEM_DEFAULTS",
    "RepairEstimate",
    "bump_class",
    "class_rank",
    "estimate_repairs",
    "is_houston",
    "normalize_issue",
    "rate_for",
]
