"""The pilot report: per-scenario grading, aggregates, invariants, POL-04 readiness, renders.

``PilotReport`` is plain data (``to_dict`` round-trips through JSON) so the CLI can write it,
``GET /pilot/report`` can serve it and tests can assert on it without re-running anything.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

Status = Literal["pass", "fail", "known_defect", "skipped"]


@dataclass(slots=True)
class Check:
    name: str
    expected: Any
    actual: Any
    ok: bool
    note: str | None = None

    def render(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        extra = f"  ({self.note})" if self.note else ""
        return f"{mark} {self.name}: expected {self.expected!r}, actual {self.actual!r}{extra}"


@dataclass(slots=True)
class ScenarioResult:
    scenario_id: str
    title: str
    market: str | None
    investor: str | None
    status: Status
    checks: list[Check] = field(default_factory=list)
    covers: tuple[str, ...] = ()
    known_defects: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


@dataclass(slots=True)
class Gap:
    """A defect or missing piece in the system under test the pilot found."""

    id: str
    severity: Literal["high", "medium", "low"]
    where: str
    behaviour: str
    expected: str
    evidence: str = ""


@dataclass(slots=True)
class Aggregates:
    markets_scored: int = 0
    markets_active: int = 0
    markets_watch: int = 0
    investors_discovered: int = 0  # investor rows written by A1 in the pilot markets
    investors_qualified: int = 0
    investors_priority: int = 0
    investors_with_email: int = 0
    sequences_started: int = 0
    sequences_completed: int = 0
    sequences_cancelled: dict[str, int] = field(default_factory=dict)  # reason -> count
    touches_sent: int = 0
    touches_sent_by_step: dict[str, int] = field(default_factory=dict)
    touches_auto_sent: int = 0
    subject_variants: dict[str, int] = field(default_factory=dict)  # INV-11 variant -> initials
    inbound_injected: int = 0
    inbound_rows: int = 0
    inbound_audit_events: int = 0
    bounces: int = 0
    classifications: dict[str, int] = field(default_factory=dict)
    auto_replies: int = 0
    buy_boxes_current: int = 0
    buy_box_versions: int = 0
    buy_box_corrections: int = 0
    investors_with_buy_box: int = 0
    suppressions: dict[str, int] = field(default_factory=dict)  # reason -> count
    reply_drafts: int = 0
    replies_sent: int = 0
    replies_auto_sent: int = 0
    replies_blocked: int = 0
    queue_items_total: int = 0
    queue_depth_end: int = 0
    approvals: int = 0
    approvals_with_edits: int = 0
    rejections: int = 0
    qa_verdicts: dict[str, int] = field(default_factory=dict)
    qa_flags: dict[str, int] = field(default_factory=dict)
    policy_decisions: dict[str, int] = field(default_factory=dict)  # "action:decision" -> n
    model_calls: int = 0
    model_calls_by_agent_task: dict[str, int] = field(default_factory=dict)
    agent_runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: str = "0"
    signal_contract_errors: int = 0
    workflows_started: int = 0
    workflows_failed: int = 0
    workflows_parked_at_end: int = 0


@dataclass(slots=True)
class PilotReport:
    mode: str  # "sim" | "temporal"
    level: str
    approve_everything: bool
    seed: int
    markets: list[str]
    started_at: str
    finished_at: str
    wall_seconds: float
    sim_start: str
    sim_end: str
    simulated_days: float
    database_url: str
    scenarios: list[ScenarioResult] = field(default_factory=list)
    invariants: list[Check] = field(default_factory=list)
    aggregates: Aggregates = field(default_factory=Aggregates)
    readiness: list[dict[str, Any]] = field(default_factory=list)
    policy_decisions: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    signal_contract_errors: list[dict[str, Any]] = field(default_factory=list)
    workflow_errors: dict[str, str] = field(default_factory=dict)
    unscripted_model_calls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- verdicts ------------------------------------------------------------------------------

    @property
    def failed(self) -> list[ScenarioResult]:
        return [s for s in self.scenarios if s.status == "fail"]

    @property
    def known_defect_scenarios(self) -> list[ScenarioResult]:
        return [s for s in self.scenarios if s.status == "known_defect"]

    @property
    def passed_scenarios(self) -> list[ScenarioResult]:
        return [s for s in self.scenarios if s.status == "pass"]

    @property
    def invariants_hold(self) -> bool:
        return all(c.ok for c in self.invariants)

    @property
    def ok(self) -> bool:
        """No scenario failed, every invariant holds, no workflow crashed."""
        return not self.failed and self.invariants_hold and not self.workflow_errors

    def ok_strict(self) -> bool:
        return self.ok and not self.known_defect_scenarios

    # -- serialisation -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["ok_strict"] = self.ok_strict()
        return data

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False, default=_jsonable)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PilotReport:
        payload = dict(data)
        payload.pop("ok", None)
        payload.pop("ok_strict", None)
        payload["scenarios"] = [
            ScenarioResult(
                **{
                    **s,
                    "checks": [Check(**c) for c in s.get("checks", [])],
                    "covers": tuple(s.get("covers", ())),
                }
            )
            for s in payload.get("scenarios", [])
        ]
        payload["invariants"] = [Check(**c) for c in payload.get("invariants", [])]
        payload["aggregates"] = Aggregates(**payload.get("aggregates", {}))
        payload["gaps"] = [Gap(**g) for g in payload.get("gaps", [])]
        return cls(**payload)

    # -- renders ---------------------------------------------------------------------------------

    def render_text(self) -> str:
        a = self.aggregates
        lines: list[str] = []
        verdict = "PASS" if self.ok else "FAIL"
        if self.ok and self.known_defect_scenarios:
            verdict = "PASS (with known defects)"
        lines.append("SYNTHETIC INVESTOR PILOT (BO-09)")
        lines.append("=" * 72)
        lines.append(
            f"verdict: {verdict}    mode: {self.mode}    level: {self.level}    "
            f"approve_everything: {self.approve_everything}    seed: {self.seed}"
        )
        lines.append(f"markets: {', '.join(self.markets)}")
        lines.append(
            f"wall time: {self.wall_seconds:.1f}s    simulated: {self.simulated_days:.1f} days "
            f"({self.sim_start} -> {self.sim_end})"
        )
        lines.append(f"database: {self.database_url}")
        lines.append("")
        lines.append("SCENARIOS")
        lines.append("-" * 72)
        for s in self.scenarios:
            tag = s.status.upper()
            where = f" [{s.market} / {s.investor}]" if s.investor else " [cohort]"
            lines.append(f"{tag:<13}{s.scenario_id}{where}")
            lines.append(f"             {s.title}")
            if s.covers:
                lines.append(f"             covers: {', '.join(s.covers)}")
            if s.skipped_reason:
                lines.append(f"             skipped: {s.skipped_reason}")
            for c in s.checks:
                if not c.ok:
                    lines.append(f"             {c.render()}")
            if s.known_defects:
                lines.append(f"             known defects: {', '.join(s.known_defects)}")
        lines.append("")
        lines.append("INVARIANTS")
        lines.append("-" * 72)
        lines.extend(c.render() for c in self.invariants)
        lines.append("")
        lines.append("AGGREGATES")
        lines.append("-" * 72)
        lines.extend(
            [
                f"markets scored {a.markets_scored} (active {a.markets_active}, "
                f"watch {a.markets_watch})",
                f"investors discovered {a.investors_discovered}, qualified "
                f"{a.investors_qualified}, priority {a.investors_priority}, with email "
                f"{a.investors_with_email}",
                f"sequences started {a.sequences_started}, completed {a.sequences_completed}, "
                f"cancelled {_fmt_hist(a.sequences_cancelled)}",
                f"touches sent {a.touches_sent} by step {_fmt_hist(a.touches_sent_by_step)}; "
                f"auto-sent {a.touches_auto_sent}; initial subject variants "
                f"{_fmt_hist(a.subject_variants)}",
                f"inbound injected {a.inbound_injected}, rows {a.inbound_rows}, received "
                f"audit events {a.inbound_audit_events}, bounces {a.bounces}, auto-replies "
                f"{a.auto_replies}",
                f"classifications {_fmt_hist(a.classifications)}",
                f"buy boxes current {a.buy_boxes_current} (versions {a.buy_box_versions}, "
                f"corrections {a.buy_box_corrections}, investors with a box "
                f"{a.investors_with_buy_box})",
                f"suppressions {_fmt_hist(a.suppressions)}",
                f"reply drafts {a.reply_drafts}, sent {a.replies_sent} (auto "
                f"{a.replies_auto_sent}), blocked {a.replies_blocked}",
                f"queue items {a.queue_items_total}, depth at end {a.queue_depth_end}; approvals "
                f"{a.approvals} (with edits {a.approvals_with_edits}), rejections {a.rejections}",
                f"QA verdicts {_fmt_hist(a.qa_verdicts)}; flags {_fmt_hist(a.qa_flags)}",
                f"policy decisions {_fmt_hist(a.policy_decisions)}",
                f"model calls {a.model_calls} {_fmt_hist(a.model_calls_by_agent_task)}; "
                f"agent_runs {a.agent_runs}; tokens in {a.input_tokens} / out "
                f"{a.output_tokens}; cost ${a.cost_usd} (mock pricing)",
                f"workflows started {a.workflows_started}, failed {a.workflows_failed}, parked "
                f"at end {a.workflows_parked_at_end}; signal contract errors "
                f"{a.signal_contract_errors}",
            ]
        )
        lines.append("")
        lines.append("POL-04 PROMOTION READINESS (per action, last 30 days)")
        lines.append("-" * 72)
        for r in self.readiness:
            crit = r.get("criteria", {})
            met = ", ".join(k for k, v in crit.items() if v) or "none"
            lines.append(
                f"{r['action']:<28} approved {r['approved_drafts']:>3}  edited "
                f"{r['edited_drafts']:>3}  qa-blocked-sent {r['qa_blocked_sent']}  "
                f"compliance-flags {r['compliance_flags']}  ready={r['ready']}  met: {met}"
            )
        if self.signal_contract_errors:
            lines.append("")
            lines.append("SIGNAL CONTRACT ERRORS (would fail the workflow task under Temporal)")
            lines.append("-" * 72)
            for e in self.signal_contract_errors:
                lines.append(
                    f"{e['workflow_id']}: signal {e['signal']!r} with payload "
                    f"{e['payload_types']} -> handler takes {e['handler_params']} "
                    f"({e['error']})"
                )
        if self.workflow_errors:
            lines.append("")
            lines.append("WORKFLOW ERRORS")
            lines.append("-" * 72)
            lines.extend(f"{wid}: {err}" for wid, err in self.workflow_errors.items())
        if self.unscripted_model_calls:
            lines.append("")
            lines.append("UNSCRIPTED MODEL CALLS (schema placeholders were returned)")
            lines.append("-" * 72)
            lines.extend(self.unscripted_model_calls)
        lines.append("")
        lines.append("GAPS FOUND IN THE SYSTEM UNDER TEST")
        lines.append("-" * 72)
        for g in self.gaps:
            lines.append(f"{g.id} [{g.severity}] {g.where}")
            lines.append(f"    behaviour: {g.behaviour}")
            lines.append(f"    expected:  {g.expected}")
            if g.evidence:
                lines.append(f"    evidence:  {g.evidence}")
        if self.notes:
            lines.append("")
            lines.append("NOTES")
            lines.append("-" * 72)
            lines.extend(self.notes)
        return "\n".join(lines) + "\n"

    def render_markdown(self) -> str:
        a = self.aggregates
        verdict = "PASS" if self.ok else "FAIL"
        if self.ok and self.known_defect_scenarios:
            verdict = "PASS (with known defects)"
        out: list[str] = []
        out.append("# Synthetic investor pilot (BO-09)")
        out.append("")
        out.append(
            f"**Verdict: {verdict}** - mode `{self.mode}`, level `{self.level}`, "
            f"approve_everything `{self.approve_everything}`, seed `{self.seed}`, markets "
            f"{', '.join(f'`{m}`' for m in self.markets)}."
        )
        out.append("")
        out.append(
            f"Wall time {self.wall_seconds:.1f}s for {self.simulated_days:.1f} simulated days "
            f"({self.sim_start} to {self.sim_end}). Database `{self.database_url}`."
        )
        out.append("")
        out.append("## Scenarios")
        out.append("")
        out.append("| Status | Scenario | Persona | Covers | Failed checks |")
        out.append("|---|---|---|---|---|")
        for s in self.scenarios:
            persona = f"{s.investor} ({s.market})" if s.investor else "cohort"
            failed = "; ".join(
                f"{c.name}: expected {c.expected!r}, actual {c.actual!r}"
                for c in s.checks
                if not c.ok
            )
            if s.skipped_reason:
                failed = f"skipped: {s.skipped_reason}"
            if s.known_defects:
                failed = (failed + " " if failed else "") + f"[known: {', '.join(s.known_defects)}]"
            out.append(
                f"| {s.status} | `{s.scenario_id}` | {persona} | {', '.join(s.covers)} | "
                f"{failed or '-'} |"
            )
        out.append("")
        out.append("## Invariants")
        out.append("")
        out.append("| Check | Expected | Actual | OK |")
        out.append("|---|---|---|---|")
        for c in self.invariants:
            out.append(f"| {c.name} | {c.expected!r} | {c.actual!r} | {'yes' if c.ok else 'NO'} |")
        out.append("")
        out.append("## Aggregates")
        out.append("")
        rows = [
            (
                "Markets scored / active / watch",
                f"{a.markets_scored} / {a.markets_active} / {a.markets_watch}",
            ),
            ("Investors discovered (rows written)", a.investors_discovered),
            (
                "Investors qualified / priority / with email",
                f"{a.investors_qualified} / {a.investors_priority} / {a.investors_with_email}",
            ),
            ("Sequences started / completed", f"{a.sequences_started} / {a.sequences_completed}"),
            ("Sequences cancelled by reason", _fmt_hist(a.sequences_cancelled)),
            ("Touches sent (by step)", f"{a.touches_sent} {_fmt_hist(a.touches_sent_by_step)}"),
            ("Touches auto-sent (L1 path)", a.touches_auto_sent),
            ("Initial subject variants (INV-11)", _fmt_hist(a.subject_variants)),
            (
                "Inbound injected / rows / received events",
                f"{a.inbound_injected} / {a.inbound_rows} / {a.inbound_audit_events}",
            ),
            ("Bounces / auto-replies", f"{a.bounces} / {a.auto_replies}"),
            ("Classifications", _fmt_hist(a.classifications)),
            (
                "Buy boxes current / versions / corrections",
                f"{a.buy_boxes_current} / {a.buy_box_versions} / {a.buy_box_corrections}",
            ),
            ("Investors with a buy box", a.investors_with_buy_box),
            ("Suppressions by reason", _fmt_hist(a.suppressions)),
            (
                "Reply drafts / sent / auto-sent / blocked",
                f"{a.reply_drafts} / {a.replies_sent} / {a.replies_auto_sent} / "
                f"{a.replies_blocked}",
            ),
            ("Queue items / depth at end", f"{a.queue_items_total} / {a.queue_depth_end}"),
            (
                "Approvals / with edits / rejections",
                f"{a.approvals} / {a.approvals_with_edits} / {a.rejections}",
            ),
            ("QA verdicts", _fmt_hist(a.qa_verdicts)),
            ("QA flags", _fmt_hist(a.qa_flags)),
            ("Policy decisions", _fmt_hist(a.policy_decisions)),
            (
                "Model calls (by agent:task)",
                f"{a.model_calls} {_fmt_hist(a.model_calls_by_agent_task)}",
            ),
            (
                "agent_runs rows / tokens in / out / cost",
                f"{a.agent_runs} / {a.input_tokens} / {a.output_tokens} / ${a.cost_usd}",
            ),
            (
                "Workflows started / failed / parked",
                f"{a.workflows_started} / {a.workflows_failed} / {a.workflows_parked_at_end}",
            ),
            ("Signal contract errors", a.signal_contract_errors),
        ]
        out.append("| Metric | Value |")
        out.append("|---|---|")
        out.extend(f"| {k} | {v} |" for k, v in rows)
        out.append("")
        out.append("## POL-04 promotion readiness")
        out.append("")
        out.append("| Action | Approved | Edited | QA-blocked sent | Compliance flags | Ready |")
        out.append("|---|---|---|---|---|---|")
        for r in self.readiness:
            out.append(
                f"| `{r['action']}` | {r['approved_drafts']} | {r['edited_drafts']} | "
                f"{r['qa_blocked_sent']} | {r['compliance_flags']} | {r['ready']} |"
            )
        if self.signal_contract_errors:
            out.append("")
            out.append("## Signal contract errors")
            out.append("")
            for e in self.signal_contract_errors:
                out.append(
                    f"- `{e['workflow_id']}`: `{e['signal']}` with payload {e['payload_types']} "
                    f"but the handler takes {e['handler_params']} argument(s): {e['error']}"
                )
        if self.workflow_errors:
            out.append("")
            out.append("## Workflow errors")
            out.append("")
            out.extend(f"- `{w}`: {e}" for w, e in self.workflow_errors.items())
        if self.unscripted_model_calls:
            out.append("")
            out.append("## Unscripted model calls")
            out.append("")
            out.extend(f"- {u}" for u in self.unscripted_model_calls)
        out.append("")
        out.append("## Gaps found in the system under test")
        out.append("")
        for g in self.gaps:
            out.append(f"### {g.id} ({g.severity}) - {g.where}")
            out.append("")
            out.append(f"- Behaviour: {g.behaviour}")
            out.append(f"- Expected: {g.expected}")
            if g.evidence:
                out.append(f"- Evidence: {g.evidence}")
            out.append("")
        if self.notes:
            out.append("## Notes")
            out.append("")
            out.extend(f"- {n}" for n in self.notes)
            out.append("")
        return "\n".join(out)


def _fmt_hist(hist: dict[str, int]) -> str:
    if not hist:
        return "{}"
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(hist.items())) + "}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


__all__ = ["Aggregates", "Check", "Gap", "PilotReport", "ScenarioResult", "Status"]
