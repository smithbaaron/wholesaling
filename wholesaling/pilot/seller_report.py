"""The seller pilot report (BO-14): per-scenario grading, aggregates, invariants, POL-04
readiness for the seller actions, text and Markdown renders.

Plain data (``to_dict`` round-trips through JSON) so the CLI writes it, ``GET
/pilot/seller-report`` serves it and tests assert on it. ``Check``, ``Gap``, ``ScenarioResult``
and ``Status`` are the investor report's (``pilot/report.py``); only the aggregates differ.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from wholesaling.pilot.report import Check, Gap, ScenarioResult, Status, _fmt_hist, _jsonable


@dataclass(slots=True)
class SellerAggregates:
    markets_active: int = 0
    s1_records_pulled: int = 0
    s1_parcels: int = 0
    s1_leads_created: int = 0
    s1_rejected_by_reason: dict[str, int] = field(default_factory=dict)
    s1_refused_markets: dict[str, str] = field(default_factory=dict)  # market -> reason
    leads_total: int = 0
    leads_by_source: dict[str, int] = field(default_factory=dict)
    leads_by_status: dict[str, int] = field(default_factory=dict)
    leads_traced: int = 0
    leads_reachable: int = 0
    leads_unreachable: int = 0
    leads_dnc_listed: int = 0
    seeded_leads: int = 0
    sequences_started: int = 0
    sequences_completed: int = 0
    sequences_cancelled: dict[str, int] = field(default_factory=dict)
    touches_sent: int = 0
    touches_sent_by_step: dict[str, int] = field(default_factory=dict)
    touches_auto_sent: int = 0
    inbound_injected: int = 0
    inbound_rows: int = 0
    form_submissions: int = 0
    form_rows: int = 0
    classifications: dict[str, int] = field(default_factory=dict)
    intake_replies: int = 0
    intake_replies_sent: int = 0
    intake_replies_auto_sent: int = 0
    intake_replies_sensitive: int = 0
    intakes: int = 0
    intakes_complete: int = 0
    photo_links_sent: int = 0
    offers_computed: int = 0
    offers_by_status: dict[str, int] = field(default_factory=dict)
    offers_by_confidence: dict[str, int] = field(default_factory=dict)
    gates_failed: dict[str, int] = field(default_factory=dict)  # gate -> offers failing it
    offers_all_gates_passed: int = 0
    verification_results: dict[str, int] = field(default_factory=dict)  # "V1:pass" -> rows
    offers_sent: int = 0
    offers_auto_sent: int = 0
    offers_queued: int = 0
    offers_blocked: int = 0
    counters: int = 0
    counters_auto_accepted: int = 0
    counters_to_human: int = 0
    offers_accepted: int = 0
    offers_declined: int = 0
    offers_expired: int = 0
    reminders_sent: int = 0
    reminders_queued: int = 0
    reengagements_sent: int = 0
    requeued_leads: int = 0
    contracts_requested: int = 0
    contracts_signed: int = 0
    contracts_closed: int = 0
    contracts_dead: int = 0
    blasts_drafted: int = 0
    blasts_sent: int = 0
    blasts_auto_sent: int = 0
    buyers_selected: int = 0
    deposits_recorded: int = 0
    realized_fees_usd: str = "0"
    suppressions: dict[str, int] = field(default_factory=dict)
    queue_items_total: int = 0
    queue_depth_end: int = 0
    approvals: int = 0
    approval_failures: int = 0
    qa_verdicts: dict[str, int] = field(default_factory=dict)
    qa_flags: dict[str, int] = field(default_factory=dict)
    policy_decisions: dict[str, int] = field(default_factory=dict)
    model_calls: int = 0
    model_calls_by_agent_task: dict[str, int] = field(default_factory=dict)
    agent_runs: int = 0
    cost_usd: str = "0"
    sms_rows: int = 0
    workflows_started: int = 0
    workflows_failed: int = 0
    workflows_parked_at_end: int = 0
    signal_contract_errors: int = 0


@dataclass(slots=True)
class SellerPilotReport:
    mode: str
    level: str
    approve_everything: bool
    seed: int
    market: str
    started_at: str
    finished_at: str
    wall_seconds: float
    sim_start: str
    sim_end: str
    simulated_days: float
    database_url: str
    scenarios: list[ScenarioResult] = field(default_factory=list)
    invariants: list[Check] = field(default_factory=list)
    aggregates: SellerAggregates = field(default_factory=SellerAggregates)
    readiness: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    signal_contract_errors: list[dict[str, Any]] = field(default_factory=list)
    workflow_errors: dict[str, str] = field(default_factory=dict)
    unscripted_model_calls: list[str] = field(default_factory=list)
    operator_log: list[dict[str, Any]] = field(default_factory=list)
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
        """Every invariant holds, except the ones a documented gap explains (``note`` starts
        with ``known defect``); those are listed but do not fail the run."""
        return all(c.ok or _known(c) for c in self.invariants)

    @property
    def known_defect_invariants(self) -> list[Check]:
        return [c for c in self.invariants if not c.ok and _known(c)]

    @property
    def ok(self) -> bool:
        return not self.failed and self.invariants_hold and not self.workflow_errors

    def ok_strict(self) -> bool:
        return self.ok and not self.known_defect_scenarios and not self.known_defect_invariants

    @property
    def verdict(self) -> str:
        if not self.ok:
            return "FAIL"
        known = self.known_defect_scenarios or self.known_defect_invariants
        return "PASS (with known defects)" if known else "PASS"

    # -- serialisation -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["ok_strict"] = self.ok_strict()
        data["kind"] = "seller"
        return data

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False, default=_jsonable)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SellerPilotReport:
        payload = dict(data)
        for key in ("ok", "ok_strict", "kind"):
            payload.pop(key, None)
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
        payload["aggregates"] = SellerAggregates(**payload.get("aggregates", {}))
        payload["gaps"] = [Gap(**g) for g in payload.get("gaps", [])]
        return cls(**payload)

    # -- renders ---------------------------------------------------------------------------------

    def _aggregate_rows(self) -> list[tuple[str, Any]]:
        a = self.aggregates
        return [
            ("Markets Active (forced by the operator, PRE-17)", a.markets_active),
            (
                "S1 records pulled / parcels / leads created",
                f"{a.s1_records_pulled} / {a.s1_parcels} / {a.s1_leads_created}",
            ),
            ("S1 rejected by reason", _fmt_hist(a.s1_rejected_by_reason)),
            ("S1 refused markets", _fmt_hist_str(a.s1_refused_markets)),
            ("Leads total (by source)", f"{a.leads_total} {_fmt_hist(a.leads_by_source)}"),
            ("Leads by final status", _fmt_hist(a.leads_by_status)),
            (
                "S2 traced / reachable / unreachable / DNC-listed",
                f"{a.leads_traced} / {a.leads_reachable} / {a.leads_unreachable} / "
                f"{a.leads_dnc_listed}",
            ),
            ("Seeded leads (GAP-S01)", a.seeded_leads),
            (
                "Sequences started / completed / cancelled",
                f"{a.sequences_started} / {a.sequences_completed} / "
                f"{_fmt_hist(a.sequences_cancelled)}",
            ),
            (
                "Touches sent (by step) / auto-sent",
                f"{a.touches_sent} {_fmt_hist(a.touches_sent_by_step)} / {a.touches_auto_sent}",
            ),
            (
                "Inbound emails injected / rows; form submissions / rows",
                f"{a.inbound_injected} / {a.inbound_rows}; {a.form_submissions} / {a.form_rows}",
            ),
            ("Classifications", _fmt_hist(a.classifications)),
            (
                "S4 intake replies drafted / sent / auto-sent / sensitive",
                f"{a.intake_replies} / {a.intake_replies_sent} / {a.intake_replies_auto_sent} / "
                f"{a.intake_replies_sensitive}",
            ),
            (
                "Intakes / complete / photo links sent",
                f"{a.intakes} / {a.intakes_complete} / {a.photo_links_sent}",
            ),
            ("Offers computed (by status)", f"{a.offers_computed} {_fmt_hist(a.offers_by_status)}"),
            ("Offers by ARV confidence", _fmt_hist(a.offers_by_confidence)),
            (
                "Gates failed (offers failing each) / offers with every gate passed",
                f"{_fmt_hist(a.gates_failed)} / {a.offers_all_gates_passed}",
            ),
            ("Verification rows by pass:result", _fmt_hist(a.verification_results)),
            (
                "Offers sent / auto-sent / queued / blocked",
                f"{a.offers_sent} / {a.offers_auto_sent} / {a.offers_queued} / {a.offers_blocked}",
            ),
            (
                "Counters / auto-accepted / to human",
                f"{a.counters} / {a.counters_auto_accepted} / {a.counters_to_human}",
            ),
            (
                "Offers accepted / declined / expired",
                f"{a.offers_accepted} / {a.offers_declined} / {a.offers_expired}",
            ),
            (
                "Reminders sent / queued; re-engagements sent; leads requeued",
                f"{a.reminders_sent} / {a.reminders_queued}; {a.reengagements_sent}; "
                f"{a.requeued_leads}",
            ),
            (
                "Contracts requested / signed / closed / dead",
                f"{a.contracts_requested} / {a.contracts_signed} / {a.contracts_closed} / "
                f"{a.contracts_dead}",
            ),
            (
                "Blasts drafted / sent / auto-sent",
                f"{a.blasts_drafted} / {a.blasts_sent} / {a.blasts_auto_sent}",
            ),
            (
                "Buyers selected / deposits / realised fees",
                f"{a.buyers_selected} / {a.deposits_recorded} / ${a.realized_fees_usd}",
            ),
            ("Suppressions by reason", _fmt_hist(a.suppressions)),
            (
                "Queue items / depth at end / approvals / approval failures",
                f"{a.queue_items_total} / {a.queue_depth_end} / {a.approvals} / "
                f"{a.approval_failures}",
            ),
            ("QA verdicts / flags", f"{_fmt_hist(a.qa_verdicts)} / {_fmt_hist(a.qa_flags)}"),
            ("Policy decisions", _fmt_hist(a.policy_decisions)),
            (
                "Model calls (by agent:task) / agent_runs / cost",
                f"{a.model_calls} {_fmt_hist(a.model_calls_by_agent_task)} / {a.agent_runs} / "
                f"${a.cost_usd}",
            ),
            ("SMS message rows (must be 0)", a.sms_rows),
            (
                "Workflows started / failed / parked; signal contract errors",
                f"{a.workflows_started} / {a.workflows_failed} / {a.workflows_parked_at_end}; "
                f"{a.signal_contract_errors}",
            ),
        ]

    def render_text(self) -> str:
        lines: list[str] = []
        lines.append("SYNTHETIC SELLER PILOT (BO-14)")
        lines.append("=" * 72)
        lines.append(
            f"verdict: {self.verdict}    mode: {self.mode}    level: {self.level}    "
            f"approve_everything: {self.approve_everything}    seed: {self.seed}"
        )
        lines.append(f"market: {self.market}")
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
        lines.extend(f"{k}: {v}" for k, v in self._aggregate_rows())
        lines.append("")
        lines.append("POL-04 PROMOTION READINESS (seller actions, last 30 days)")
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
        out: list[str] = []
        out.append("# Synthetic seller pilot (BO-14)")
        out.append("")
        out.append(
            f"**Verdict: {self.verdict}** - mode `{self.mode}`, level `{self.level}`, "
            f"approve_everything `{self.approve_everything}`, seed `{self.seed}`, market "
            f"`{self.market}`."
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
        out.append("| Metric | Value |")
        out.append("|---|---|")
        out.extend(f"| {k} | {v} |" for k, v in self._aggregate_rows())
        out.append("")
        out.append("## POL-04 promotion readiness (seller actions)")
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


def _known(check: Check) -> bool:
    return bool(check.note and check.note.startswith("known defect"))


def _fmt_hist_str(hist: dict[str, str]) -> str:
    if not hist:
        return "{}"
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(hist.items())) + "}"


__all__ = ["Check", "Gap", "ScenarioResult", "SellerAggregates", "SellerPilotReport", "Status"]
