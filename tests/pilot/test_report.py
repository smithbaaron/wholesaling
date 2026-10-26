"""PilotReport: verdict logic, JSON round trip and the two renders."""

from __future__ import annotations

import json

from wholesaling.pilot.report import Aggregates, Check, Gap, PilotReport, ScenarioResult


def _report(**over: object) -> PilotReport:
    base = {
        "mode": "sim",
        "level": "L0",
        "approve_everything": True,
        "seed": 1,
        "markets": ["Houston"],
        "started_at": "2026-10-25T00:00:00+00:00",
        "finished_at": "2026-10-25T00:00:05+00:00",
        "wall_seconds": 5.0,
        "sim_start": "2026-10-27T15:00:00+00:00",
        "sim_end": "2026-11-10T15:00:00+00:00",
        "simulated_days": 14.0,
        "database_url": "sqlite+aiosqlite:///:memory:",
    }
    base.update(over)
    return PilotReport(**base)  # type: ignore[arg-type]


def _scenario(status: str, *failed: str) -> ScenarioResult:
    checks = [Check("touches_sent", 4, 4, True)] + [Check(n, 1, 0, False) for n in failed]
    return ScenarioResult(
        scenario_id=f"s_{status}",
        title="t",
        market="Houston",
        investor="Acme",
        status=status,  # type: ignore[arg-type]
        checks=checks,
        covers=("INV-10",),
        known_defects=["GAP-02"] if status == "known_defect" else [],
    )


def test_verdict_rules() -> None:
    report = _report(scenarios=[_scenario("pass")], invariants=[Check("i", 1, 1, True)])
    assert report.ok and report.ok_strict()
    report.scenarios.append(_scenario("known_defect", "inbound_rows"))
    assert report.ok and not report.ok_strict()
    report.scenarios.append(_scenario("fail", "touches_sent"))
    assert not report.ok and report.failed[0].failed_checks[0].name == "touches_sent"
    clean = _report(scenarios=[_scenario("pass")], invariants=[Check("i", 1, 2, False)])
    assert not clean.ok and not clean.invariants_hold
    crashed = _report(scenarios=[_scenario("pass")], workflow_errors={"wf": "boom"})
    assert not crashed.ok


def test_json_round_trip_keeps_scenarios_checks_and_gaps() -> None:
    report = _report(
        scenarios=[_scenario("pass"), _scenario("known_defect", "inbound_rows")],
        invariants=[Check("INV-24", 3, 3, True)],
        aggregates=Aggregates(sequences_started=3, classifications={"question": 1}),
        gaps=[Gap("GAP-X", "low", "here", "b", "e", "ev")],
        readiness=[
            {
                "action": "investor_reply",
                "approved_drafts": 2,
                "edited_drafts": 0,
                "qa_blocked_sent": 0,
                "compliance_flags": 0,
                "ready": False,
                "criteria": {"approved_drafts": False},
            }
        ],
    )
    data = json.loads(report.to_json())
    assert data["ok"] is True and data["ok_strict"] is False
    back = PilotReport.from_dict(data)
    assert back.aggregates.sequences_started == 3
    assert back.scenarios[1].known_defects == ["GAP-02"]
    assert back.scenarios[1].checks[1].name == "inbound_rows"
    assert back.gaps[0].id == "GAP-X" and back.invariants[0].ok
    assert back.ok == report.ok


def test_renders_mention_every_section_and_failure() -> None:
    report = _report(
        scenarios=[_scenario("fail", "buy_box_versions")],
        invariants=[Check("INV-23", [], ["x"], False)],
        gaps=[Gap("GAP-01", "high", "where", "behaviour", "expected")],
        signal_contract_errors=[
            {
                "workflow_id": "wf",
                "signal": "reply",
                "payload_types": ["ReplySignal"],
                "handler_params": 0,
                "error": "boom",
            }
        ],
        notes=["a note"],
    )
    text = report.render_text()
    md = report.render_markdown()
    for needle in ("FAIL", "buy_box_versions", "INV-23", "GAP-01", "ReplySignal", "a note"):
        assert needle in text, needle
        assert needle in md, needle
    assert "POL-04" in text and "## POL-04 promotion readiness" in md
    assert md.startswith("# Synthetic investor pilot (BO-09)")
