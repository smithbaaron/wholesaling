"""``SellerPilotReport``: verdicts, known-defect handling for scenarios and invariants, JSON
round trip, text and Markdown renders."""

from __future__ import annotations

import json

from wholesaling.pilot.report import Check, Gap, ScenarioResult
from wholesaling.pilot.seller_report import SellerAggregates, SellerPilotReport


def _report() -> SellerPilotReport:
    return SellerPilotReport(
        mode="sim",
        level="L0",
        approve_everything=True,
        seed=1,
        market="Houston",
        started_at="2026-10-25T00:00:00+00:00",
        finished_at="2026-10-25T00:00:07+00:00",
        wall_seconds=7.0,
        sim_start="2026-10-27T15:00:00+00:00",
        sim_end="2027-01-27T15:00:00+00:00",
        simulated_days=92.0,
        database_url="sqlite+aiosqlite:///:memory:",
        scenarios=[
            ScenarioResult(
                scenario_id="web_form_full_to_close",
                title="full close",
                market="Houston",
                investor="Pat Doe",
                status="known_defect",
                checks=[
                    Check("offer_sent", True, True, True),
                    Check("gates_failed", [], ["no_open_flags"], False),
                ],
                covers=("SEL-11",),
                known_defects=["GAP-S03"],
            ),
            ScenarioResult(
                scenario_id="seed_no_reply",
                title="no reply",
                market="Houston",
                investor="Quiet Owner",
                status="pass",
                checks=[Check("touches_sent", 4, 4, True)],
            ),
        ],
        invariants=[
            Check("POL-03e no SMS", 0, 0, True),
            Check(
                "SEL-05 canonical disclosure",
                [],
                ["m1"],
                False,
                note="known defect GAP-S12: A7 templates carry a second wording",
            ),
        ],
        aggregates=SellerAggregates(offers_computed=12, contracts_closed=1, sms_rows=0),
        readiness=[
            {
                "action": "offer_send",
                "approved_drafts": 11,
                "edited_drafts": 0,
                "qa_blocked_sent": 0,
                "compliance_flags": 0,
                "ready": False,
                "criteria": {"enough_approvals": False},
            }
        ],
        gaps=[Gap("GAP-S03", "medium", "sell.py", "no co-owner question", "ask it", "9 offers")],
        notes=["mock everything"],
    )


def test_verdict_counts_known_defects_as_passing_unless_strict() -> None:
    report = _report()
    assert report.ok and not report.ok_strict()
    assert report.verdict == "PASS (with known defects)"
    assert [s.scenario_id for s in report.known_defect_scenarios] == ["web_form_full_to_close"]
    assert [c.name for c in report.known_defect_invariants] == ["SEL-05 canonical disclosure"]
    assert report.invariants_hold
    # An unexplained invariant failure is a FAIL.
    report.invariants.append(Check("no workflow crashed", [], ["offer:x"], False))
    assert not report.invariants_hold and not report.ok and report.verdict == "FAIL"


def test_a_failed_scenario_fails_the_report() -> None:
    report = _report()
    report.scenarios[1].status = "fail"
    report.scenarios[1].checks[0].ok = False
    assert report.failed and not report.ok and report.verdict == "FAIL"


def test_json_round_trip_keeps_every_section() -> None:
    report = _report()
    data = json.loads(report.to_json())
    assert data["ok"] is True and data["ok_strict"] is False and data["kind"] == "seller"
    back = SellerPilotReport.from_dict(data)
    assert back.to_dict() == report.to_dict()
    assert back.aggregates.offers_computed == 12
    assert back.invariants[1].note is not None and back.invariants[1].note.startswith(
        "known defect"
    )
    assert back.gaps[0].id == "GAP-S03"


def test_text_and_markdown_renders_carry_the_verdict_scenarios_gaps_and_readiness() -> None:
    report = _report()
    text = report.render_text()
    assert text.startswith("SYNTHETIC SELLER PILOT (BO-14)")
    assert "verdict: PASS (with known defects)" in text
    assert "KNOWN_DEFECT web_form_full_to_close" in text
    assert "FAIL gates_failed: expected [], actual ['no_open_flags']" in text
    assert "known defects: GAP-S03" in text
    assert "GAP-S03 [medium] sell.py" in text
    assert "offer_send" in text and "approved  11" in text
    assert "SMS message rows (must be 0): 0" in text
    md = report.render_markdown()
    assert md.startswith("# Synthetic seller pilot (BO-14)")
    assert "| known_defect | `web_form_full_to_close` | Pat Doe (Houston) |" in md
    assert "### GAP-S03 (medium) - sell.py" in md
    assert "| `offer_send` | 11 | 0 | 0 | 0 | False |" in md
    assert "- mock everything" in md
