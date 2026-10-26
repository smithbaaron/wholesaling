"""Scoring primitives for golden cases.

Every check returns a ``Check`` carrying the expected and actual values so a failure prints as
a diff. A case passes when every check passes; its ``score`` is the fraction of checks passed
(reported, not gated: the gate is pass / fail per case).

- ``exact``          equality after normalising Decimals / enums / dates to strings
- ``set_f1``         precision / recall / F1 over two sets (flags, fields, comp ids); passes
                     at ``threshold`` (default 1.0 = identical sets)
- ``number_within``  numeric closeness, absolute or relative tolerance
- ``contains`` / ``excludes``  substring checks on prompt or draft text
- ``never_invented`` the fields that must be empty are empty (INV-14 / SEL-06: nothing the
                     counterpart did not state may appear in the record)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": _jsonable(self.expected),
            "actual": _jsonable(self.actual),
            "detail": self.detail,
        }

    def diff(self) -> str:
        if self.passed:
            return f"ok   {self.name}"
        tail = f" ({self.detail})" if self.detail else ""
        return (
            f"FAIL {self.name}: expected {_jsonable(self.expected)!r}, "
            f"got {_jsonable(self.actual)!r}{tail}"
        )


@dataclass(slots=True)
class SetScore:
    precision: float
    recall: float
    f1: float
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [_jsonable(v) for v in value]
        return sorted(items, key=str) if isinstance(value, set | frozenset) else items
    return value


def normalize(value: Any) -> Any:
    """Canonical form for equality: Decimals / enums / dates to strings, lists elementwise."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return normalize(value.value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return normalize(Decimal(str(value)))
    if isinstance(value, str):
        try:
            # "250000" and 250000 compare equal; "sfr" stays "sfr".
            return format(Decimal(value).normalize(), "f") if _looks_numeric(value) else value
        except InvalidOperation:
            return value
    if isinstance(value, Mapping):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    return value


def _looks_numeric(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    body = stripped[1:] if stripped[0] in "+-" else stripped
    return body.replace(".", "", 1).isdigit()


def exact(name: str, expected: Any, actual: Any, *, detail: str = "") -> Check:
    return Check(
        name=name,
        passed=normalize(expected) == normalize(actual),
        expected=expected,
        actual=actual,
        detail=detail,
    )


def set_score(expected: Iterable[Any], actual: Iterable[Any]) -> SetScore:
    e = {str(normalize(x)) for x in expected}
    a = {str(normalize(x)) for x in actual}
    tp = len(e & a)
    precision = tp / len(a) if a else (1.0 if not e else 0.0)
    recall = tp / len(e) if e else (1.0 if not a else 0.0)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return SetScore(
        precision=precision,
        recall=recall,
        f1=f1,
        missing=sorted(e - a),
        extra=sorted(a - e),
    )


def set_f1(
    name: str, expected: Iterable[Any], actual: Iterable[Any], *, threshold: float = 1.0
) -> Check:
    e_list, a_list = list(expected), list(actual)
    score = set_score(e_list, a_list)
    detail = f"f1={score.f1:.2f}"
    if score.missing:
        detail += f" missing={score.missing}"
    if score.extra:
        detail += f" extra={score.extra}"
    return Check(
        name=name,
        passed=score.f1 >= threshold,
        expected=sorted(str(normalize(x)) for x in e_list),
        actual=sorted(str(normalize(x)) for x in a_list),
        detail=detail,
    )


def subset(name: str, required: Iterable[Any], actual: Iterable[Any]) -> Check:
    """Every required member is present (extra members are allowed)."""
    req = {str(normalize(x)) for x in required}
    got = {str(normalize(x)) for x in actual}
    missing = sorted(req - got)
    return Check(
        name=name,
        passed=not missing,
        expected=sorted(req),
        actual=sorted(got),
        detail=f"missing={missing}" if missing else "",
    )


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def number_within(
    name: str,
    expected: Any,
    actual: Any,
    *,
    abs_tol: Decimal | float | int = 0,
    rel_tol: Decimal | float | int = 0,
) -> Check:
    """Both ``None`` passes; one ``None`` fails; otherwise |a - e| <= max(abs, rel * |e|)."""
    e, a = _to_decimal(expected), _to_decimal(actual)
    if e is None and a is None:
        return Check(name=name, passed=True, expected=expected, actual=actual)
    if e is None or a is None:
        return Check(
            name=name, passed=False, expected=expected, actual=actual, detail="one side is null"
        )
    allowed = max(Decimal(str(abs_tol)), Decimal(str(rel_tol)) * abs(e))
    delta = abs(a - e)
    return Check(
        name=name,
        passed=delta <= allowed,
        expected=e,
        actual=a,
        detail=f"delta={delta} tolerance={allowed}",
    )


def contains(name: str, needle: str, haystack: str) -> Check:
    return Check(
        name=name,
        passed=needle in haystack,
        expected=f"contains {needle!r}",
        actual=haystack[:200],
    )


def excludes(name: str, needle: str, haystack: str) -> Check:
    return Check(
        name=name,
        passed=needle not in haystack,
        expected=f"excludes {needle!r}",
        actual=haystack[:200],
    )


def never_invented(name: str, must_be_empty: Iterable[str], values: Mapping[str, Any]) -> Check:
    """Fields the counterpart did not state must be empty in the record."""
    invented = {
        f: values.get(f) for f in must_be_empty if values.get(f) not in (None, "", [], {}, ())
    }
    return Check(
        name=name,
        passed=not invented,
        expected={f: None for f in must_be_empty},
        actual=invented or {f: None for f in must_be_empty},
        detail=f"invented={sorted(invented)}" if invented else "",
    )


def score_of(checks: Iterable[Check]) -> float:
    items = list(checks)
    if not items:
        return 0.0
    return sum(1 for c in items if c.passed) / len(items)


__all__ = [
    "Check",
    "SetScore",
    "contains",
    "exact",
    "excludes",
    "never_invented",
    "normalize",
    "number_within",
    "score_of",
    "set_f1",
    "set_score",
    "subset",
]
