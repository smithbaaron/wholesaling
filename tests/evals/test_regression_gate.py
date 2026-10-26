"""The BO-13 regression gate as a test: run every committed golden case in scripted mode and
assert nothing regressed against ``wholesaling/evals/baseline.json`` (CI runs pytest, so this is
the CI step). A live run is attempted only when ``ANTHROPIC_API_KEY`` is set and is reported,
not gated (the baseline is scripted-mode data)."""

from __future__ import annotations

import os

import pytest

from wholesaling.config.settings import Settings
from wholesaling.evals.cases import GoldenCase, load_all_cases
from wholesaling.evals.report import BASELINE_PATH, build_report, compare_to_baseline, load_baseline
from wholesaling.evals.runners import live_available, run_all


async def test_scripted_run_does_not_regress_against_the_committed_baseline(
    all_cases: list[GoldenCase], settings: Settings
) -> None:
    results = await run_all(all_cases, settings=settings, mode="mock")
    report = build_report(results, mode="mock")
    gate = compare_to_baseline(report, load_baseline(BASELINE_PATH))
    details = "\n".join(r.diff() for r in report.results if not r.passed)
    assert gate.ok, f"{gate.summary()}\n{details}"
    assert gate.missing_agents == [], "every baselined agent must still have cases"


def test_baseline_covers_every_committed_case() -> None:
    """Adding cases is fine; the baseline totals must not exceed what is committed."""
    baseline = load_baseline(BASELINE_PATH)
    counts: dict[str, int] = {}
    for c in load_all_cases():
        counts[c.agent] = counts.get(c.agent, 0) + 1
    for agent, entry in baseline["agents"].items():
        assert counts.get(agent, 0) >= entry["total"], agent
        assert entry["total"] == counts[agent], (
            f"{agent}: {counts[agent]} committed cases vs {entry['total']} in the baseline; "
            "run `wholesaling-evals run --update-baseline` after adding cases"
        )


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("EVALS_LIVE") != "1",
    reason="live evals need ANTHROPIC_API_KEY and EVALS_LIVE=1 (they spend money)",
)
async def test_live_run_reports_pass_rates(all_cases: list[GoldenCase]) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert live_available(settings)
    subset = [c for c in all_cases if c.agent == "A4" and c.task == "classify"][:5]
    results = await run_all(subset, settings=settings, mode="anthropic")
    report = build_report(results, mode="anthropic")
    assert report.total == len(subset)
    print(report.to_markdown())
