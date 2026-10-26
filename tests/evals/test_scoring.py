"""Scoring primitives: exact match with normalisation, set-F1, numeric tolerance, text checks,
never-invent."""

from __future__ import annotations

from decimal import Decimal

from wholesaling.db.models.enums import MessageClassification
from wholesaling.evals.scoring import (
    contains,
    exact,
    excludes,
    never_invented,
    normalize,
    number_within,
    score_of,
    set_f1,
    set_score,
    subset,
)


def test_exact_normalises_decimals_enums_and_numeric_strings() -> None:
    assert exact("a", "250000", Decimal("250000.00")).passed
    assert exact("b", 250000, "250000").passed
    assert exact("c", "interested", MessageClassification.INTERESTED).passed
    assert exact("d", ["sfr"], ["sfr"]).passed
    assert not exact("e", ["sfr"], ["SFR"]).passed, "exact is case-sensitive for text"
    assert normalize({"x": Decimal("1.50")}) == {"x": "1.5"}
    assert normalize("not a number") == "not a number"
    assert normalize(True) is True


def test_check_diff_text() -> None:
    ok = exact("verdict", "approve", "approve")
    bad = exact("verdict", "approve", "block", detail="why")
    assert ok.diff().startswith("ok ")
    assert bad.diff() == "FAIL verdict: expected 'approve', got 'block' (why)"
    assert bad.to_json()["passed"] is False


def test_set_f1_and_set_score() -> None:
    score = set_score(["a", "b"], ["b", "c"])
    assert score.precision == 0.5 and score.recall == 0.5 and score.f1 == 0.5
    assert score.missing == ["a"] and score.extra == ["c"]
    assert set_f1("flags", ["a", "b"], ["b", "a"]).passed
    assert not set_f1("flags", ["a", "b"], ["b", "c"]).passed
    assert set_f1("flags", ["a", "b"], ["b", "c"], threshold=0.5).passed
    assert set_f1("empty", [], []).passed
    assert not set_f1("empty_vs_some", [], ["x"]).passed
    assert set_score([], []).f1 == 1.0


def test_subset() -> None:
    assert subset("s", ["a"], ["a", "b"]).passed
    missing = subset("s", ["a", "z"], ["a"])
    assert not missing.passed and "missing=['z']" in missing.detail


def test_number_within_tolerances() -> None:
    assert number_within("n", "185000", Decimal("185000")).passed
    assert not number_within("n", "185000", "185001").passed
    assert number_within("n", "185000", "185001", abs_tol=1).passed
    assert number_within("n", 100, 104, rel_tol=0.05).passed
    assert not number_within("n", 100, 106, rel_tol=0.05).passed
    assert number_within("both_null", None, None).passed
    assert not number_within("one_null", None, 5).passed
    assert not number_within("garbage", "abc", 5).passed


def test_text_checks() -> None:
    assert contains("c", "150k", "I said 150k max").passed
    assert not contains("c", "250k", "I said 150k max").passed
    assert excludes("e", "250k", "I said 150k max").passed
    assert not excludes("e", "150k", "I said 150k max").passed


def test_never_invented() -> None:
    values = {"condition": None, "financing": "cash", "timeline": [], "hoa": ""}
    assert never_invented("ok", ["condition", "timeline", "hoa"], values).passed
    bad = never_invented("bad", ["condition", "financing"], values)
    assert not bad.passed and bad.detail == "invented=['financing']"
    assert bad.actual == {"financing": "cash"}


def test_score_of() -> None:
    assert score_of([]) == 0.0
    assert score_of([exact("a", 1, 1), exact("b", 1, 2)]) == 0.5
