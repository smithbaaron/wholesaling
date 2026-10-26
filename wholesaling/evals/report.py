"""Eval report and the baseline regression gate (BO-13 deliverable 3).

``EvalReport`` groups ``CaseResult``s per agent (total, passed, pass rate, failing ids,
findings). ``baseline.json`` next to this module records the committed pass rates in scripted
mode; ``compare_to_baseline`` fails when any agent's pass rate drops below its baseline or a
case that passed in the baseline now fails (a "new failure"). Cases the baseline already lists as
failing are findings, not regressions, so a known agent defect does not block CI while staying
visible in every report.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from wholesaling.evals.runners import CaseResult, ProviderMode

BASELINE_PATH: Final[Path] = Path(__file__).resolve().parent / "baseline.json"
BASELINE_VERSION: Final = 1
RATE_EPSILON: Final = 1e-9


@dataclass(slots=True)
class Finding:
    """A golden expectation the agent does not meet (the agent is not changed; this is recorded)."""

    case_id: str
    agent: str
    task: str
    summary: str
    tags: tuple[str, ...] = ()
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "agent": self.agent,
            "task": self.task,
            "summary": self.summary,
            "tags": list(self.tags),
            "notes": self.notes,
        }


@dataclass(slots=True)
class AgentSummary:
    agent: str
    total: int
    passed: int
    failing_ids: list[str] = field(default_factory=list)
    errored_ids: list[str] = field(default_factory=list)
    model_calls: int = 0

    @property
    def pass_rate(self) -> float:
        return 0.0 if self.total == 0 else self.passed / self.total

    def to_json(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "failing_ids": list(self.failing_ids),
            "errored_ids": list(self.errored_ids),
            "model_calls": self.model_calls,
        }


@dataclass(slots=True)
class EvalReport:
    mode: ProviderMode
    generated_at: datetime
    results: list[CaseResult]
    agents: dict[str, AgentSummary]
    findings: list[Finding]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return 0.0 if not self.results else self.passed / self.total

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "generated_at": self.generated_at.isoformat(),
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "agents": {a: s.to_json() for a, s in sorted(self.agents.items())},
            "findings": [f.to_json() for f in self.findings],
            "results": [r.to_json() for r in self.results],
        }

    def to_markdown(self, *, include_passes: bool = False) -> str:
        lines = [
            f"# Agent eval report ({self.mode} mode)",
            "",
            f"Generated {self.generated_at.isoformat()} - {self.passed}/{self.total} cases pass"
            f" ({self.pass_rate:.1%}).",
            "",
            "| Agent | Cases | Passed | Pass rate | Model calls | Failing |",
            "|---|---|---|---|---|---|",
        ]
        for agent, s in sorted(self.agents.items()):
            failing = ", ".join(s.failing_ids) or "-"
            lines.append(
                f"| {agent} | {s.total} | {s.passed} | {s.pass_rate:.1%} | "
                f"{s.model_calls} | {failing} |"
            )
        lines.append("")
        lines.append("## Findings")
        lines.append("")
        if not self.findings:
            lines.append("None: every golden expectation is met.")
        for f in self.findings:
            lines.append(f"- **{f.agent}/{f.task} `{f.case_id}`** - {f.summary}")
            if f.notes:
                lines.append(f"  - note: {f.notes}")
        lines.append("")
        lines.append("## Cases")
        lines.append("")
        for r in self.results:
            if r.passed and not include_passes:
                continue
            lines.append("```")
            lines.append(r.diff())
            lines.append("```")
        return "\n".join(lines).rstrip() + "\n"

    def baseline_document(self) -> dict[str, Any]:
        """What ``baseline.json`` holds: per-agent totals and the ids that fail."""
        return {
            "version": BASELINE_VERSION,
            "mode": self.mode,
            "generated_at": self.generated_at.isoformat(),
            "agents": {
                a: {
                    "total": s.total,
                    "passed": s.passed,
                    "pass_rate": round(s.pass_rate, 4),
                    "failing_ids": list(s.failing_ids),
                }
                for a, s in sorted(self.agents.items())
            },
        }


def _finding_summary(result: CaseResult) -> str:
    if result.error:
        first = result.error.strip().splitlines()[0] if result.error.strip() else "error"
        return f"runner error: {first}"
    failed = result.failed_checks
    parts = [f"{c.name}: expected {c.expected!r}, got {c.actual!r}" for c in failed[:3]]
    more = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
    return "; ".join(parts) + more


def build_report(
    results: Iterable[CaseResult], *, mode: ProviderMode, now: datetime | None = None
) -> EvalReport:
    items = list(results)
    agents: dict[str, AgentSummary] = {}
    findings: list[Finding] = []
    for r in items:
        s = agents.setdefault(r.agent, AgentSummary(agent=r.agent, total=0, passed=0))
        s.total += 1
        s.model_calls += r.model_calls
        if r.passed:
            s.passed += 1
        else:
            s.failing_ids.append(r.case_id)
            if r.error:
                s.errored_ids.append(r.case_id)
            findings.append(
                Finding(
                    case_id=r.case_id,
                    agent=r.agent,
                    task=r.task,
                    summary=_finding_summary(r),
                    tags=r.tags,
                    notes=r.notes,
                )
            )
    return EvalReport(
        mode=mode,
        generated_at=now or datetime.now(tz=UTC),
        results=items,
        agents=agents,
        findings=findings,
    )


# --------------------------------------------------------------------------------------------
# Baseline gate
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class RegressionResult:
    ok: bool
    regressions: list[str] = field(default_factory=list)  # human-readable reasons
    new_failures: list[str] = field(default_factory=list)  # case ids
    fixed: list[str] = field(default_factory=list)  # baseline failures that now pass
    missing_agents: list[str] = field(default_factory=list)  # in baseline, absent from the run

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "regressions": list(self.regressions),
            "new_failures": list(self.new_failures),
            "fixed": list(self.fixed),
            "missing_agents": list(self.missing_agents),
        }

    def summary(self) -> str:
        if self.ok:
            tail = f"; {len(self.fixed)} baseline failure(s) now pass" if self.fixed else ""
            return f"no regression against the baseline{tail}"
        return "REGRESSION: " + "; ".join(self.regressions)


def load_baseline(path: Path | None = None) -> dict[str, Any]:
    doc = json.loads((path or BASELINE_PATH).read_text())
    if not isinstance(doc, dict) or "agents" not in doc:
        msg = f"{path or BASELINE_PATH}: not a baseline document"
        raise ValueError(msg)
    return doc


def write_baseline(report: EvalReport, path: Path | None = None) -> Path:
    target = path or BASELINE_PATH
    target.write_text(json.dumps(report.baseline_document(), indent=2, sort_keys=True) + "\n")
    return target


def compare_to_baseline(
    report: EvalReport,
    baseline: Mapping[str, Any],
    *,
    agents: Sequence[str] | None = None,
) -> RegressionResult:
    """Gate: every agent in the run must hold its baseline pass rate and no case that passed in
    the baseline may fail now. ``agents`` restricts the comparison to a subset (``--agent``)."""
    out = RegressionResult(ok=True)
    base_agents: Mapping[str, Any] = baseline.get("agents") or {}
    wanted = {a.upper() for a in agents} if agents else None
    for agent, base in base_agents.items():
        if wanted is not None and agent not in wanted:
            continue
        summary = report.agents.get(agent)
        if summary is None:
            out.missing_agents.append(agent)
            continue
        base_rate = float(base.get("pass_rate", 0.0))
        base_failing = set(base.get("failing_ids") or [])
        now_failing = set(summary.failing_ids)
        new = sorted(now_failing - base_failing)
        fixed = sorted(base_failing - now_failing)
        out.fixed.extend(f"{agent}:{cid}" for cid in fixed)
        if new:
            out.new_failures.extend(f"{agent}:{cid}" for cid in new)
            out.regressions.append(f"{agent}: new failing case(s) {new}")
        if summary.pass_rate + RATE_EPSILON < base_rate:
            out.regressions.append(
                f"{agent}: pass rate {summary.pass_rate:.1%} below baseline {base_rate:.1%}"
            )
    out.ok = not out.regressions
    return out


__all__ = [
    "BASELINE_PATH",
    "BASELINE_VERSION",
    "AgentSummary",
    "EvalReport",
    "Finding",
    "RegressionResult",
    "build_report",
    "compare_to_baseline",
    "load_baseline",
    "write_baseline",
]
