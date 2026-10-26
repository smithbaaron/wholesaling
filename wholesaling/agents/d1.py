"""D1 - search profile engine (REQUIREMENTS.md BB-04, BB-05, BB-06, WF-07, WF-20).

Turns one buy-box version into normalised, hashable search criteria:

- geography: markets as ``City, ST``, cities title-cased, 5-digit ZIPs, neighbourhoods;
- property classes from the investor's words (``sfr``, ``small_multi``, ``condo_townhome``,
  ``land``, ``commercial``; anything else kept as a slug so nothing stated is dropped);
- price_min / price_max as decimal strings; condition tolerance as the worst class accepted
  (``light`` < ``medium`` < ``heavy`` < ``gut``); financing, strategy, closing preferences and
  every other explicit constraint carried through.

The investor's original wording is kept on the profile (``original_wording``) but excluded
from the hash, so re-stating the same criteria in different words coalesces (BB-05) while any
structural change supersedes the active profile with a new version (BB-04). Unknowns (fields
asked but not answered, plus required fields that are missing) travel with the profile so D2 can
flag them as data gaps instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.repositories.deals import criteria_hash

PROFILE_VERSION = "d1-2026-10-25.1"
ORIGINAL_WORDING_KEY = "original_wording"

CLASS_SFR = "sfr"
CLASS_SMALL_MULTI = "small_multi"
CLASS_CONDO_TOWNHOME = "condo_townhome"
CLASS_LAND = "land"
CLASS_COMMERCIAL = "commercial"

_CLASS_ALIASES: dict[str, str] = {
    "sfr": CLASS_SFR,
    "sfh": CLASS_SFR,
    "single family": CLASS_SFR,
    "single-family": CLASS_SFR,
    "single family home": CLASS_SFR,
    "single family homes": CLASS_SFR,
    "house": CLASS_SFR,
    "houses": CLASS_SFR,
    "residential": CLASS_SFR,
    "duplex": CLASS_SMALL_MULTI,
    "duplexes": CLASS_SMALL_MULTI,
    "triplex": CLASS_SMALL_MULTI,
    "fourplex": CLASS_SMALL_MULTI,
    "quad": CLASS_SMALL_MULTI,
    "quadplex": CLASS_SMALL_MULTI,
    "multi": CLASS_SMALL_MULTI,
    "multifamily": CLASS_SMALL_MULTI,
    "multi-family": CLASS_SMALL_MULTI,
    "multi family": CLASS_SMALL_MULTI,
    "small multi": CLASS_SMALL_MULTI,
    "small multifamily": CLASS_SMALL_MULTI,
    "2-4 units": CLASS_SMALL_MULTI,
    "2-4 unit": CLASS_SMALL_MULTI,
    "condo": CLASS_CONDO_TOWNHOME,
    "condos": CLASS_CONDO_TOWNHOME,
    "condominium": CLASS_CONDO_TOWNHOME,
    "townhome": CLASS_CONDO_TOWNHOME,
    "townhomes": CLASS_CONDO_TOWNHOME,
    "townhouse": CLASS_CONDO_TOWNHOME,
    "townhouses": CLASS_CONDO_TOWNHOME,
    "land": CLASS_LAND,
    "lot": CLASS_LAND,
    "lots": CLASS_LAND,
    "vacant land": CLASS_LAND,
    "commercial": CLASS_COMMERCIAL,
    "retail": CLASS_COMMERCIAL,
    "office": CLASS_COMMERCIAL,
}

CONDITION_ORDER: tuple[str, ...] = ("light", "medium", "heavy", "gut")
_CONDITION_ALIASES: dict[str, str] = {
    "light": "light",
    "cosmetic": "light",
    "turnkey": "light",
    "turn-key": "light",
    "move-in ready": "light",
    "rent ready": "light",
    "medium": "medium",
    "moderate": "medium",
    "some work": "medium",
    "heavy": "heavy",
    "major": "heavy",
    "major rehab": "heavy",
    "full rehab": "heavy",
    "distressed": "heavy",
    "gut": "gut",
    "gut rehab": "gut",
    "any": "gut",
    "any condition": "gut",
    "all": "gut",
    "anything": "gut",
    "teardown": "gut",
}

_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_WS = re.compile(r"\s+")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


def normalize_property_class(raw: str) -> str:
    text = _WS.sub(" ", raw.strip().lower())
    if text in _CLASS_ALIASES:
        return _CLASS_ALIASES[text]
    for alias, canonical in _CLASS_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return canonical
    return _slug(text)


def normalize_condition(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    text = _WS.sub(" ", raw.strip().lower())
    if text in _CONDITION_ALIASES:
        return _CONDITION_ALIASES[text]
    worst: str | None = None
    for alias, canonical in _CONDITION_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text) and (
            worst is None or CONDITION_ORDER.index(canonical) > CONDITION_ORDER.index(worst)
        ):
            worst = canonical
    return worst


def normalize_zip(raw: str) -> str | None:
    m = _ZIP.search(str(raw))
    return m.group(1) if m else None


def normalize_city(raw: str) -> str:
    return " ".join(w.capitalize() for w in _WS.sub(" ", raw.strip()).split(" "))


def normalize_market(raw: str) -> str:
    text = _WS.sub(" ", raw.strip())
    if "," in text:
        city, _, state = text.partition(",")
        return f"{normalize_city(city)}, {state.strip().upper()[:2]}"
    return normalize_city(text)


def _money(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(Decimal(str(value)).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class NormalizedProfile:
    criteria: dict[str, Any]
    unknowns: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return hashable_hash(self.criteria)


def hashable_criteria(criteria: dict[str, Any]) -> dict[str, Any]:
    """The criteria minus the wording-only keys (what BB-05 compares)."""
    return {k: v for k, v in criteria.items() if k not in (ORIGINAL_WORDING_KEY, "profile_version")}


def hashable_hash(criteria: dict[str, Any]) -> str:
    return criteria_hash(hashable_criteria(criteria))


class D1SearchProfileEngine:
    """Pure normalisation; the activity persists through ``SearchProfileRepository``."""

    def normalize(self, buy_box: BuyBox) -> NormalizedProfile:
        geo = dict(buy_box.geography or {})
        markets = sorted(
            {normalize_market(str(m)) for m in geo.get("markets") or [] if str(m).strip()}
        )
        cities = sorted({normalize_city(str(c)) for c in geo.get("cities") or [] if str(c).strip()})
        zips = sorted({z for z in (normalize_zip(str(x)) for x in geo.get("zips") or []) if z})
        neighborhoods = sorted(
            {normalize_city(str(n)) for n in geo.get("neighborhoods") or [] if str(n).strip()}
        )
        classes = sorted(
            {normalize_property_class(str(a)) for a in buy_box.asset_types or [] if str(a).strip()}
        )
        condition = normalize_condition(buy_box.condition)
        price_min = _money(buy_box.price_min)
        price_max = _money(buy_box.price_max)
        constraints = {
            _slug(str(k)): v
            for k, v in sorted((buy_box.other_criteria or {}).items(), key=lambda kv: str(kv[0]))
        }
        closing = {
            _slug(str(k)): v
            for k, v in sorted(
                (buy_box.closing_preferences or {}).items(), key=lambda kv: str(kv[0])
            )
        }
        unknowns = sorted({str(u) for u in (buy_box.unknown_fields or [])})
        if not (markets or cities or zips or neighborhoods):
            unknowns.append("geography")
        if not classes:
            unknowns.append("property_classes")
        if price_max is None:
            unknowns.append("price_max")
        if condition is None:
            unknowns.append("condition")
        unknowns = sorted(set(unknowns))
        criteria: dict[str, Any] = {
            "profile_version": PROFILE_VERSION,
            "buy_box_key": buy_box.buy_box_key,
            "geography": {
                "markets": markets,
                "cities": cities,
                "zips": zips,
                "neighborhoods": neighborhoods,
            },
            "property_classes": classes,
            "price_min": price_min,
            "price_max": price_max,
            "condition_tolerance": condition,
            "strategy": (buy_box.strategy or "").strip().lower() or None,
            "financing": (buy_box.financing or "").strip().lower() or None,
            "closing": closing,
            "constraints": constraints,
            "unknowns": unknowns,
            ORIGINAL_WORDING_KEY: buy_box.raw_text,
        }
        return NormalizedProfile(criteria=criteria, unknowns=unknowns)


def condition_within(tolerance: str | None, candidate_hint: str | None) -> bool | None:
    """True / False when both known; ``None`` when either side is unknown."""
    if tolerance is None or candidate_hint is None:
        return None
    hint = normalize_condition(candidate_hint)
    if hint is None:
        return None
    return CONDITION_ORDER.index(hint) <= CONDITION_ORDER.index(tolerance)


__all__ = [
    "CLASS_COMMERCIAL",
    "CLASS_CONDO_TOWNHOME",
    "CLASS_LAND",
    "CLASS_SFR",
    "CLASS_SMALL_MULTI",
    "CONDITION_ORDER",
    "ORIGINAL_WORDING_KEY",
    "PROFILE_VERSION",
    "D1SearchProfileEngine",
    "NormalizedProfile",
    "condition_within",
    "hashable_criteria",
    "hashable_hash",
    "normalize_city",
    "normalize_condition",
    "normalize_market",
    "normalize_property_class",
    "normalize_zip",
]
