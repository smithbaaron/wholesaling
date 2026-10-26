"""Report aggregation, findings, and the baseline regression gate (BO-13 #3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from wholesaling.evals.report import (
    BASELINE_PATH,
    build_report,
    compare_to_baseline,
    load_baseline,
    write_baseline,
)
from wholesaling.evals.runners import CaseResult
from wholesaling.evals.scoring import exact

NOW = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)


def _result(
    agent: str, case_id: str, *, passed: bool = True, error: str | None = None
) -> CaseResult:
    r = CaseResult(case_id=case_id, agent=agent, task="t", mode="mock", model_calls=1, tags=("x",))
    r.checks = [exact("a", 1, 1 if passed else 2), exact("b", 1, 1)]
    r.error = error
    return r


def test_build_report_groups_per_agent_and_lists_findings() -> None:
    results = [
        _result("A4", "ok1"),
        _result("A4", "bad", passed=False),
        _result("A5", "ok2"),
        _result("A7", "boom", error="RuntimeError: x\n  trace"),
    ]
    report = build_report(results, mode="mock", now=NOW)
    assert report.total == 4 and report.passed == 2 and report.pass_rate == 0.5
    assert report.agents["A4"].pass_rate == 0.5 and report.agents["A4"].failing_ids == ["bad"]
    assert report.agents["A5"].pass_rate == 1.0 and report.agents["A5"].model_calls == 1
    assert report.agents["A7"].errored_ids == ["boom"]
    summaries = {f.case_id: f.summary for f in report.findings}
    assert summaries["bad"].startswith("a: expected 1, got 2")
    assert summaries["boom"] == "runner error: RuntimeError: x"
    doc = report.to_json()
    assert doc["agents"]["A4"]["failing_ids"] == ["bad"] and len(doc["results"]) == 4
    md = report.to_markdown()
    assert "| A4 | 2 | 1 | 50.0% | 2 | bad |" in md
    assert "**A4/t `bad`**" in md and "FAIL A4/t bad" in md and "PASS A4/t ok1" not in md
    assert "PASS A4/t ok1" in report.to_markdown(include_passes=True)
    assert build_report([], mode="mock", now=NOW).pass_rate == 0.0
    assert "None: every golden expectation is met." in build_report([], mode="mock").to_markdown()


def test_baseline_document_roundtrip(tmp_path: Path) -> None:
    report = build_report(
        [_result("A4", "ok"), _result("A4", "bad", passed=False)], mode="mock", now=NOW
    )
    path = write_baseline(report, tmp_path / "baseline.json")
    doc = load_baseline(path)
    assert doc["version"] == 1 and doc["mode"] == "mock"
    assert doc["agents"]["A4"] == {
        "total": 2,
        "passed": 1,
        "pass_rate": 0.5,
        "failing_ids": ["bad"],
    }


def test_gate_passes_when_nothing_regressed() -> None:
    baseline = {"agents": {"A4": {"pass_rate": 1.0, "failing_ids": []}}}
    report = build_report([_result("A4", "ok")], mode="mock", now=NOW)
    gate = compare_to_baseline(report, baseline)
    assert gate.ok and gate.summary() == "no regression against the baseline"


def test_gate_fails_on_a_new_failure_and_a_rate_drop() -> None:
    baseline = {"agents": {"A4": {"pass_rate": 1.0, "failing_ids": []}}}
    report = build_report(
        [_result("A4", "ok"), _result("A4", "bad", passed=False)], mode="mock", now=NOW
    )
    gate = compare_to_baseline(report, baseline)
    assert not gate.ok
    assert gate.new_failures == ["A4:bad"]
    assert any("new failing case" in r for r in gate.regressions)
    assert any("pass rate 50.0% below baseline 100.0%" in r for r in gate.regressions)
    assert gate.summary().startswith("REGRESSION")
    assert gate.to_json()["new_failures"] == ["A4:bad"]


def test_known_failure_in_the_baseline_is_a_finding_not_a_regression() -> None:
    baseline = {"agents": {"A4": {"pass_rate": 0.5, "failing_ids": ["bad"]}}}
    report = build_report(
        [_result("A4", "ok"), _result("A4", "bad", passed=False)], mode="mock", now=NOW
    )
    gate = compare_to_baseline(report, baseline)
    assert gate.ok and gate.new_failures == []
    assert [f.case_id for f in report.findings] == ["bad"]


def test_fixed_cases_are_reported_and_a_different_failure_still_regresses() -> None:
    baseline = {"agents": {"A4": {"pass_rate": 0.5, "failing_ids": ["bad"]}}}
    fixed = build_report([_result("A4", "ok"), _result("A4", "bad")], mode="mock", now=NOW)
    gate = compare_to_baseline(fixed, baseline)
    assert gate.ok and gate.fixed == ["A4:bad"] and "now pass" in gate.summary()
    swapped = build_report(
        [_result("A4", "ok", passed=False), _result("A4", "bad")], mode="mock", now=NOW
    )
    gate = compare_to_baseline(swapped, baseline)
    assert not gate.ok and gate.new_failures == ["A4:ok"]


def test_gate_restricted_to_agents_ignores_missing_ones() -> None:
    baseline = {
        "agents": {
            "A4": {"pass_rate": 1.0, "failing_ids": []},
            "A5": {"pass_rate": 1.0, "failing_ids": []},
        }
    }
    report = build_report([_result("A5", "ok")], mode="mock", now=NOW)
    full = compare_to_baseline(report, baseline)
    assert full.ok and full.missing_agents == ["A4"]
    only = compare_to_baseline(report, baseline, agents=["a5"])
    assert only.ok and only.missing_agents == []


def test_committed_baseline_is_well_formed() -> None:
    doc = load_baseline(BASELINE_PATH)
    assert doc["mode"] == "mock" and set(doc["agents"]) == {"A4", "A5", "A7", "S4"}
    for agent in doc["agents"].values():
        assert agent["total"] >= agent["passed"] >= 0
        assert len(agent["failing_ids"]) == agent["total"] - agent["passed"]
    assert json.loads(BASELINE_PATH.read_text()) == doc
