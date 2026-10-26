"""Step 7 - sanity gates (REQUIREMENTS.md A7-08, OPN-16).

All seven are required for auto-send; each result (pass/fail, the value tested and the threshold)
is persisted on ``offers.gates``. The gate list is evaluated in code, never by a model.

| gate                      | rule                                                         |
|---------------------------|--------------------------------------------------------------|
| arv_confidence            | ARV confidence >= Medium (Low -> human)                      |
| market_hard_cap           | offer <= market hard cap (OPN-16: deal band upper bound)     |
| min_pct_of_arv            | offer >= 35% of ARV                                          |
| asking_tolerance          | asking blank, or |offer - asking| / asking <= 25%            |
| no_open_flags             | none of: title_lien, hoa, multi_unit, non_residential,       |
|                           | probate_without_authority, co_owner_missing                  |
| seller_not_suppressed     | the seller's address is not on the suppression list          |
| no_active_offer_on_parcel | no other offer on this parcel in draft/pending/sent/countered|

``derive_open_flags`` turns intake answers and resolved parcel facts into the flag list; the
engine also appends flags raised by the verification passes (for example an unresolved facts
conflict) before the gates run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from wholesaling.config.tunables import A7Config
from wholesaling.db.models.enums import ArvConfidence

GATE_ARV_CONFIDENCE = "arv_confidence"
GATE_MARKET_HARD_CAP = "market_hard_cap"
GATE_MIN_PCT_OF_ARV = "min_pct_of_arv"
GATE_ASKING_TOLERANCE = "asking_tolerance"
GATE_NO_OPEN_FLAGS = "no_open_flags"
GATE_SELLER_NOT_SUPPRESSED = "seller_not_suppressed"
GATE_NO_ACTIVE_OFFER = "no_active_offer_on_parcel"

GATE_NAMES: tuple[str, ...] = (
    GATE_ARV_CONFIDENCE,
    GATE_MARKET_HARD_CAP,
    GATE_MIN_PCT_OF_ARV,
    GATE_ASKING_TOLERANCE,
    GATE_NO_OPEN_FLAGS,
    GATE_SELLER_NOT_SUPPRESSED,
    GATE_NO_ACTIVE_OFFER,
)

FLAG_TITLE_LIEN = "title_lien"
FLAG_HOA = "hoa"
FLAG_MULTI_UNIT = "multi_unit"
FLAG_NON_RESIDENTIAL = "non_residential"
FLAG_PROBATE_WITHOUT_AUTHORITY = "probate_without_authority"
FLAG_CO_OWNER_MISSING = "co_owner_missing"

OPEN_FLAGS: tuple[str, ...] = (
    FLAG_TITLE_LIEN,
    FLAG_HOA,
    FLAG_MULTI_UNIT,
    FLAG_NON_RESIDENTIAL,
    FLAG_PROBATE_WITHOUT_AUTHORITY,
    FLAG_CO_OWNER_MISSING,
)

RESIDENTIAL_TYPES = frozenset({"sfr", "duplex", "triplex", "fourplex", "condo", "townhouse"})
_NEGATIVE = re.compile(r"^\s*(no|none|n/a|na|nothing|not applicable|0)\b[.\s]*$", re.I)
_AUTHORITY = re.compile(
    r"\b(executor|executrix|administrator|letters testamentary|independent administration"
    r"|heirship affidavit|court order)\b",
    re.I,
)
_PROBATE = re.compile(r"\b(probate|inherited|deceased|estate of|passed away)\b", re.I)

_Q4 = Decimal("0.0001")


def _is_negative(text: str | None) -> bool:
    return text is None or not text.strip() or bool(_NEGATIVE.match(text))


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    value: Any
    threshold: Any
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "value": _json_value(self.value),
            "threshold": _json_value(self.threshold),
            "detail": self.detail,
        }


def _json_value(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, list | tuple):
        return [_json_value(x) for x in v]
    if isinstance(v, ArvConfidence):
        return v.value
    return v


@dataclass(frozen=True, slots=True)
class GateInputs:
    arv: Decimal
    arv_confidence: ArvConfidence
    offer: Decimal
    hard_cap: Decimal
    asking_price: Decimal | None
    open_flags: list[str]
    seller_suppressed: bool
    active_offer_on_parcel: bool


@dataclass(frozen=True, slots=True)
class FlagInputs:
    """What ``derive_open_flags`` looks at (intake answers + resolved facts)."""

    liens: str | None = None
    liens_unknown: bool = True
    hoa: str | None = None
    hoa_unknown: bool = True
    co_owners: str | None = None
    co_owners_unknown: bool = True
    motivation: str | None = None
    disclosed_issues: list[str] = field(default_factory=list)
    property_type: str | None = None
    units: int | None = None


def derive_open_flags(inp: FlagInputs) -> list[str]:
    """A7-08 open flags. Unknown liens / HOA are *not* flags (they are data gaps recorded on
    the calc record); a stated lien or HOA is. ``co_owner_missing`` fires when the co-owner
    question is unanswered or names co-owners who have not been identified."""
    flags: list[str] = []
    if not inp.liens_unknown and not _is_negative(inp.liens):
        flags.append(FLAG_TITLE_LIEN)
    if not inp.hoa_unknown and not _is_negative(inp.hoa):
        flags.append(FLAG_HOA)
    if inp.units is not None and inp.units > 1:
        flags.append(FLAG_MULTI_UNIT)
    if inp.property_type is not None and inp.property_type.lower() not in RESIDENTIAL_TYPES:
        flags.append(FLAG_NON_RESIDENTIAL)
    text = " ".join([inp.motivation or "", *inp.disclosed_issues])
    if _PROBATE.search(text) and not _AUTHORITY.search(text):
        flags.append(FLAG_PROBATE_WITHOUT_AUTHORITY)
    if inp.co_owners_unknown or (
        inp.co_owners
        and not _is_negative(inp.co_owners)
        and "identified" not in inp.co_owners.lower()
    ):
        flags.append(FLAG_CO_OWNER_MISSING)
    return flags


def evaluate_gates(config: A7Config, inp: GateInputs) -> list[GateResult]:
    min_pct = Decimal(str(config.min_offer_pct_of_arv))
    tolerance = Decimal(str(config.asking_tolerance_pct))
    results: list[GateResult] = []

    conf_ok = inp.arv_confidence in (ArvConfidence.HIGH, ArvConfidence.MEDIUM)
    results.append(
        GateResult(
            GATE_ARV_CONFIDENCE,
            conf_ok,
            inp.arv_confidence,
            ">= Medium",
            "ARV confidence must be High or Medium; Low goes to a human (A7-04)",
        )
    )
    results.append(
        GateResult(
            GATE_MARKET_HARD_CAP,
            inp.offer <= inp.hard_cap,
            inp.offer,
            inp.hard_cap,
            "offer <= market hard cap (OPN-16)",
        )
    )
    floor_ = (inp.arv * min_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    results.append(
        GateResult(
            GATE_MIN_PCT_OF_ARV,
            inp.offer >= floor_ and inp.offer > 0,
            inp.offer,
            floor_,
            f"offer >= {min_pct:.0%} of ARV",
        )
    )
    if inp.asking_price is None or inp.asking_price <= 0:
        results.append(
            GateResult(GATE_ASKING_TOLERANCE, True, None, tolerance, "asking price blank")
        )
    else:
        deviation = (abs(inp.offer - inp.asking_price) / inp.asking_price).quantize(
            _Q4, rounding=ROUND_HALF_UP
        )
        results.append(
            GateResult(
                GATE_ASKING_TOLERANCE,
                deviation <= tolerance,
                deviation,
                tolerance,
                f"|offer - asking| / asking <= {tolerance:.0%}",
            )
        )
    results.append(
        GateResult(
            GATE_NO_OPEN_FLAGS,
            not inp.open_flags,
            list(inp.open_flags),
            [],
            "no open title/lien, HOA, multi-unit, non-residential, probate or co-owner flags",
        )
    )
    results.append(
        GateResult(
            GATE_SELLER_NOT_SUPPRESSED,
            not inp.seller_suppressed,
            inp.seller_suppressed,
            False,
            "seller not on the suppression list",
        )
    )
    results.append(
        GateResult(
            GATE_NO_ACTIVE_OFFER,
            not inp.active_offer_on_parcel,
            inp.active_offer_on_parcel,
            False,
            "no other active offer on the same parcel",
        )
    )
    return results


def all_passed(gates: list[GateResult]) -> bool:
    return bool(gates) and all(g.passed for g in gates)


def failed_names(gates: list[GateResult]) -> list[str]:
    return [g.name for g in gates if not g.passed]


__all__ = [
    "FLAG_CO_OWNER_MISSING",
    "FLAG_HOA",
    "FLAG_MULTI_UNIT",
    "FLAG_NON_RESIDENTIAL",
    "FLAG_PROBATE_WITHOUT_AUTHORITY",
    "FLAG_TITLE_LIEN",
    "GATE_ARV_CONFIDENCE",
    "GATE_ASKING_TOLERANCE",
    "GATE_MARKET_HARD_CAP",
    "GATE_MIN_PCT_OF_ARV",
    "GATE_NAMES",
    "GATE_NO_ACTIVE_OFFER",
    "GATE_NO_OPEN_FLAGS",
    "GATE_SELLER_NOT_SUPPRESSED",
    "OPEN_FLAGS",
    "RESIDENTIAL_TYPES",
    "FlagInputs",
    "GateInputs",
    "GateResult",
    "all_passed",
    "derive_open_flags",
    "evaluate_gates",
    "failed_names",
]
