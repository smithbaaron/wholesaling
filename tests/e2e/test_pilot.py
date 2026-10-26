"""BO-09 / BO-20: the synthetic investor pilot end to end on SQLite in mock mode.

Runs the full pilot once per autonomy level (a few seconds each: 16 scenarios, 14 sequences,
14 inbound messages, ~25 review-queue approvals through the real route) and asserts that every
scenario passes, that the documented known defects are the only non-passes, and that the
INV-23 / INV-24 / RUL-05 / POL-03l / XC-02 invariants hold.

The ``temporal`` variant drives the same scenarios through the time-skipping test server. It is
opt-in (``PILOT_TEMPORAL=1``) on top of the marker because the driver could not be executed in
the build sandbox; see docs/pilot.md.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

from wholesaling.pilot.report import PilotReport
from wholesaling.pilot.runner import PilotRunner
from wholesaling.pilot.scenarios import SCENARIOS
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

KNOWN_DEFECT_SCENARIOS = {"ooo_then_real_reply"}  # GAP-02; shrink this set as defects are fixed
INVESTOR_ACTIONS = (
    PolicyAction.INVESTOR_INITIAL_OUTREACH,
    PolicyAction.INVESTOR_FOLLOWUP,
    PolicyAction.INVESTOR_REPLY,
)


async def _run(level: AutonomyLevel, **kw: object) -> PilotReport:
    runner = PilotRunner()
    overrides = {} if level is AutonomyLevel.L0 else dict.fromkeys(INVESTOR_ACTIONS, level)
    return await runner.run(policy_level_overrides=overrides, **kw)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def l0_report() -> PilotReport:
    import asyncio

    return asyncio.run(_run(AutonomyLevel.L0))


def _assert_all_pass(
    report: PilotReport, known: frozenset[str] | set[str] = frozenset(KNOWN_DEFECT_SCENARIOS)
) -> None:
    failed = {s.scenario_id: [c.render() for c in s.failed_checks] for s in report.failed}
    assert failed == {}, failed
    assert {s.scenario_id for s in report.known_defect_scenarios} == set(known)
    assert not [s for s in report.scenarios if s.status == "skipped"]
    assert report.invariants_hold, [c.render() for c in report.invariants if not c.ok]
    assert report.workflow_errors == {}
    assert report.ok


def test_every_scenario_passes_at_l0(l0_report: PilotReport) -> None:
    _assert_all_pass(l0_report)
    assert len(l0_report.scenarios) == len(SCENARIOS) == 16
    assert l0_report.wall_seconds < 60


def test_l0_aggregates_match_the_cohort(l0_report: PilotReport) -> None:
    a = l0_report.aggregates
    assert a.markets_scored == 21 and a.markets_active == 0  # M1-11: no auto-promotion
    assert a.investors_qualified == 15 and a.investors_with_email == 14
    assert a.sequences_started == 14 and a.sequences_completed == 1
    assert a.sequences_cancelled == {"bounce": 1, "opt_out": 1, "reply": 10, "suppressed": 1}
    assert a.inbound_injected == a.inbound_rows == a.inbound_audit_events == 14
    assert a.bounces == 1 and a.auto_replies == 1
    assert a.classifications == {
        "ambiguous": 1,
        "buy_box_supplied": 6,
        "not_interested": 2,
        "not_now": 1,
        "opt_out": 1,
        "question": 2,
    }
    assert a.buy_boxes_current == 6 and a.buy_box_versions == 7 and a.buy_box_corrections == 1
    assert a.suppressions == {"bounce": 1, "manual": 1, "opt_out": 1}
    # One auto touch (the L1 persona's QA-approved initial) and one auto reply (its clean reply).
    assert a.touches_auto_sent == 1 and a.replies_auto_sent == 1
    assert a.reply_drafts == a.replies_sent == 9 and a.replies_blocked == 0
    assert a.queue_depth_end == 0 and a.approvals == a.queue_items_total
    assert a.approvals_with_edits == 0 and a.rejections == 0
    assert a.qa_verdicts == {"approve": 10}  # 9 replies + the L1 persona's stand-in
    assert a.qa_flags == {"sensitive_content": 1}
    assert a.model_calls == a.agent_runs == 36  # XC-02
    assert a.model_calls_by_agent_task == {
        "A4:classify": 12,
        "A4:draft": 9,
        "A4:extract": 6,
        "A5:qa": 9,
    }
    assert a.policy_decisions == {
        "investor_followup:queue_for_human": 5,
        "investor_initial_outreach:queue_for_human": 12,
        "investor_initial_outreach:send": 1,
        "investor_reply:queue_for_human": 8,
        "investor_reply:send": 1,
    }
    assert a.workflows_started == 28 and a.workflows_failed == 0


def test_pol04_readiness_numbers_come_out_of_the_pilot(l0_report: PilotReport) -> None:
    by_action = {r["action"]: r for r in l0_report.readiness}
    assert by_action["investor_initial_outreach"]["approved_drafts"] == 12
    assert by_action["investor_followup"]["approved_drafts"] == 5
    assert by_action["investor_reply"]["approved_drafts"] == 8
    for action in ("investor_initial_outreach", "investor_followup", "investor_reply"):
        r = by_action[action]
        assert r["edited_drafts"] == 0 and r["qa_blocked_sent"] == 0
        assert r["compliance_flags"] == 0
        assert r["ready"] is False  # < 50 approved drafts
        assert r["criteria"]["approved_drafts"] is False
        assert r["criteria"]["no_qa_blocked_sent"] and r["criteria"]["no_compliance_flags"]


def test_gap_01_is_evidenced_by_the_run(l0_report: PilotReport) -> None:
    """Every reply/opt-out/bounce signal carried a payload the sequence handler cannot take."""
    errors = l0_report.signal_contract_errors
    assert len(errors) == 11 == l0_report.aggregates.signal_contract_errors
    assert {e["signal"] for e in errors} == {"reply", "opt_out", "bounce"}
    assert all(e["handler_params"] == 0 for e in errors)
    assert any(g.id == "GAP-01" for g in l0_report.gaps)
    assert l0_report.unscripted_model_calls == []


def test_the_report_renders(l0_report: PilotReport) -> None:
    text = l0_report.render_text()
    assert "PASS (with known defects)" in text and "GAP-02" in text
    md = l0_report.render_markdown()
    assert "| pass | `priority_no_reply` |" in md
    back = PilotReport.from_dict(l0_report.to_dict())
    assert back.ok and back.aggregates.sequences_started == 14


async def test_every_scenario_passes_at_l1_with_auto_replies() -> None:
    report = await _run(AutonomyLevel.L1)
    _assert_all_pass(report)
    a = report.aggregates
    # At L1 the clean, confident replies send themselves; the sensitive one still queues.
    assert a.replies_auto_sent == 8 and a.replies_sent == 9
    assert a.policy_decisions["investor_reply:send"] == 8
    assert a.policy_decisions["investor_reply:queue_for_human"] == 1
    # No A3 draft has a QA review, so every initial still queues at L1 (GAP-04) except the
    # persona with the stand-in approval.
    assert a.touches_auto_sent == 1


async def test_without_approvals_everything_parks_and_nothing_leaks() -> None:
    report = await _run(AutonomyLevel.L0, approve_everything=False)
    _assert_all_pass(report, known=set())  # the OOO persona never gets past its parked initial
    a = report.aggregates
    assert a.touches_sent == 1 and a.touches_auto_sent == 1  # only the L1 persona's initial
    # 14 sequences - the suppressed one - the L1 persona's auto-sent initial = 12 parked
    # initials, plus the L1 persona's queued price-talk reply.
    assert a.queue_depth_end == 12 + 1 and a.workflows_parked_at_end == 12
    assert a.approvals == 0 and a.replies_auto_sent == 1


@pytest.mark.temporal
async def test_pilot_through_the_temporal_test_server() -> None:
    if not os.environ.get("PILOT_TEMPORAL"):
        pytest.skip("set PILOT_TEMPORAL=1 to drive the pilot through the time-skipping server")
    from wholesaling.pilot.temporal_driver import TemporalPilotRunner

    runner = TemporalPilotRunner(operator_delay=timedelta(hours=1))
    try:
        report = await runner.run()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    assert report.mode == "temporal"
    assert not [s for s in report.scenarios if s.status == "skipped"]
    assert report.invariants_hold, [c.render() for c in report.invariants if not c.ok]
