"""BO-14 / BO-20: the synthetic seller pilot end to end on SQLite in mock mode.

Runs the full seller loop once at L0 and once at L1 (about seven seconds each: 17 scenarios,
7 seller sequences through the production starter, 12 scripted seller emails and 9 web-form
posts, 12 A7 offers with their V1..V6 rows, 3 negotiations to ``contract.requested``, 2 contracts
through the real ``/deals`` routes to a close and a dead deal, ~93 simulated days) and asserts
that every scenario passes or fails only on a documented gap, that the invariants hold (modulo
the one documented disclosure-wording gap) and that no workflow crashed.

The gaps the pilot evidences are the point of BO-14; ``KNOWN_DEFECT_SCENARIOS`` and
``KNOWN_DEFECT_INVARIANTS`` keep this test honest about them (docs/pilot.md, "Seller pilot").
"""

from __future__ import annotations

import asyncio

import pytest

from wholesaling.pilot.seller_report import SellerPilotReport
from wholesaling.pilot.seller_runner import SellerPilotRunner
from wholesaling.pilot.seller_scenarios import SELLER_SCENARIOS
from wholesaling.policy.actions import AutonomyLevel

KNOWN_DEFECT_SCENARIOS: dict[str, set[str]] = {
    "s1_full_reply_counter_inside_room": {"GAP-S09"},
    "s1_offer_expires_then_reengage": {"GAP-S05"},  # L0 only: reminders queue, cannot be approved
    "seed_asks_for_a_number": {"GAP-S08"},
    "web_form_full_to_close": {"GAP-S03", "GAP-S11"},
    "web_form_avm_gap": {"GAP-S03"},
    "web_form_pre_send_drift": {"GAP-S03"},
    "web_form_photos_missing": {"GAP-S03"},
}
KNOWN_DEFECT_INVARIANTS = {
    "SEL-05 / CMP-04 every sent seller message carries the canonical TX disclosure",  # GAP-S12
}


def _run(level: AutonomyLevel) -> SellerPilotReport:
    return asyncio.run(SellerPilotRunner().run(level=level))


@pytest.fixture(scope="module")
def l0_report() -> SellerPilotReport:
    return _run(AutonomyLevel.L0)


@pytest.fixture(scope="module")
def l1_report() -> SellerPilotReport:
    return _run(AutonomyLevel.L1)


def _assert_passes_modulo_known_gaps(report: SellerPilotReport, *, level: AutonomyLevel) -> None:
    failed = {s.scenario_id: [c.render() for c in s.failed_checks] for s in report.failed}
    assert failed == {}, failed
    known = {s.scenario_id: set(s.known_defects) for s in report.known_defect_scenarios}
    expected = dict(KNOWN_DEFECT_SCENARIOS)
    if level is AutonomyLevel.L1:
        expected.pop("s1_offer_expires_then_reengage")  # reminders auto-send at L1
    assert known == expected, known
    assert not [s for s in report.scenarios if s.status == "skipped"]
    assert {c.name for c in report.known_defect_invariants} == KNOWN_DEFECT_INVARIANTS
    assert report.invariants_hold, [c.render() for c in report.invariants if not c.ok]
    assert report.workflow_errors == {}
    assert report.signal_contract_errors == []
    assert report.unscripted_model_calls == []
    assert report.ok and not report.ok_strict()
    assert report.wall_seconds < 90


def test_every_scenario_passes_or_names_its_gap_at_l0(l0_report: SellerPilotReport) -> None:
    _assert_passes_modulo_known_gaps(l0_report, level=AutonomyLevel.L0)
    assert len(l0_report.scenarios) == len(SELLER_SCENARIOS) == 17
    assert 90 <= l0_report.simulated_days <= 95


def test_l0_aggregates_match_the_cohort(l0_report: SellerPilotReport) -> None:
    a = l0_report.aggregates
    assert a.markets_active == 1 and a.s1_refused_markets == {"Columbus": "market_not_active"}
    assert a.s1_records_pulled == 11 and a.s1_parcels == 10 and a.s1_leads_created == 5
    assert a.leads_by_source == {"mock_distress_lists": 5, "pilot_seed": 3, "web_form": 8}
    assert a.leads_traced == 5 and a.leads_reachable == 4 and a.leads_unreachable == 1
    assert a.leads_dnc_listed == 2 and a.seeded_leads == 3
    assert a.sequences_started == 7 and a.sequences_completed == 1
    assert a.sequences_cancelled == {"opt_out": 1, "reply": 5}
    assert a.touches_sent == 10 and a.touches_auto_sent == 0
    assert a.inbound_injected == a.inbound_rows == 12 and a.form_submissions == a.form_rows == 9
    assert a.classifications == {"interested": 20, "opt_out": 1}
    assert a.intake_replies == 3 and a.intake_replies_sent == 2 and a.intake_replies_sensitive == 2
    assert a.intakes_complete == 12 and a.photo_links_sent == 2
    assert a.offers_computed == 12
    assert a.offers_by_status == {"accepted": 3, "blocked": 1, "declined": 1, "expired": 7}
    assert a.offers_by_confidence == {"High": 4, "Low": 2, "Medium": 6}
    assert a.gates_failed == {"arv_confidence": 2, "min_pct_of_arv": 1, "no_open_flags": 8}
    assert a.offers_all_gates_passed == 3
    # A7-18: every pass runs; the injected V5 failure and the escalation are the only non-passes.
    assert a.verification_results["V5:fail"] == 1 and a.verification_results["V1:escalate"] == 1
    assert a.verification_results["V6:adjusted"] == 1 and a.verification_results["V2:adjusted"] == 2
    assert a.offers_sent == 11 and a.offers_auto_sent == 0 and a.offers_blocked == 1
    assert a.counters == 2 and a.counters_to_human == 2 and a.counters_auto_accepted == 0
    assert a.offers_accepted == 3 and a.offers_declined == 1 and a.offers_expired == 7
    # GAP-S05: at L0 every reminder queues and none can be approved.
    assert a.reminders_sent == 0 and a.reminders_queued == 21 and a.reengagements_sent == 0
    assert a.approval_failures == 35
    assert a.requeued_leads == 1
    assert a.contracts_requested == 3 and a.contracts_signed == 2
    assert a.contracts_closed == 1 and a.contracts_dead == 1
    assert a.blasts_drafted == a.blasts_sent == 6 and a.blasts_auto_sent == 0
    assert a.buyers_selected == 1 and a.deposits_recorded == 1
    assert a.realized_fees_usd == "10000.00"
    assert a.suppressions == {"opt_out": 1}
    assert a.sms_rows == 0
    assert a.model_calls == a.agent_runs == 104
    assert a.workflows_failed == 0 and a.workflows_parked_at_end == 0
    assert a.signal_contract_errors == 0


def test_l0_readiness_numbers_come_out_of_the_audit_trail(l0_report: SellerPilotReport) -> None:
    by_action = {r["action"]: r for r in l0_report.readiness}
    assert set(by_action) == {
        "seller_outreach_email",
        "seller_intake_reply",
        "offer_send",
        "counter_acceptance",
        "buyer_disposition_blast",
    }
    assert by_action["seller_outreach_email"]["approved_drafts"] == 10
    assert by_action["offer_send"]["approved_drafts"] == 11
    assert by_action["buyer_disposition_blast"]["approved_drafts"] == 6
    assert by_action["seller_intake_reply"]["approved_drafts"] == 2
    assert all(not r["ready"] for r in l0_report.readiness)  # 50 approvals is the bar


def test_l0_gaps_are_the_documented_ones(l0_report: SellerPilotReport) -> None:
    assert [g.id for g in l0_report.gaps] == [
        "GAP-S01",
        "GAP-S02",
        "GAP-S03",
        "GAP-S04",
        "GAP-S05",
        "GAP-S06",
        "GAP-S07",
        "GAP-S08",
        "GAP-S09",
        "GAP-S10",
        "GAP-S11",
        "GAP-S12",
    ]


def test_every_scenario_passes_or_names_its_gap_at_l1(l1_report: SellerPilotReport) -> None:
    _assert_passes_modulo_known_gaps(l1_report, level=AutonomyLevel.L1)
    assert l1_report.level == "L1"


def test_l1_automates_exactly_what_the_policy_table_allows(l1_report: SellerPilotReport) -> None:
    a = l1_report.aggregates
    # POL-03d: every QA-approved seller step under the daily cap auto-sends.
    assert a.touches_sent == a.touches_auto_sent == 10
    # POL-03f: the non-sensitive intake reply auto-sends; the price question stays human.
    assert a.intake_replies_auto_sent == 1 and a.intake_replies_sent == 2
    # POL-03g: only the three offers with every gate passed auto-send; the rest queue.
    assert a.offers_auto_sent == 3 and a.offers_queued == 8 and a.offers_sent == 11
    # POL-03h: the counter inside the room is accepted by S5; above MAO still goes to a human.
    assert a.counters_auto_accepted == 1 and a.counters_to_human == 1
    # POL-03k: every blast for a signed contract auto-sends.
    assert a.blasts_auto_sent == a.blasts_sent == 6
    # S5 reminders/re-engagements auto-send only for the offer whose gates all passed.
    assert a.reminders_sent == 3 and a.reengagements_sent == 2 and a.reminders_queued == 18
    # Same deals, same closings: autonomy level changes who clicks, not what happens.
    assert a.contracts_closed == 1 and a.contracts_dead == 1 and a.realized_fees_usd == "10000.00"
    assert a.offers_by_status == l1_report.aggregates.offers_by_status
    assert a.sms_rows == 0 and a.signal_contract_errors == 0


def test_the_human_floor_held_at_both_levels(
    l0_report: SellerPilotReport, l1_report: SellerPilotReport
) -> None:
    for report in (l0_report, l1_report):
        by_name = {c.name: c for c in report.invariants}
        assert by_name["POL-03i/j no automated actor touched contracts or earnest money"].ok
        assert by_name["SEL-11 every signed contract names a human signer"].ok
        assert by_name["POL-03e / RUL-06 no SMS row and no SMS policy decision anywhere"].ok
        assert by_name["A7-16 V5 matches the engine unless the pilot injected the bug"].ok
        assert by_name["A7-17 every sent offer has a V6 row"].ok
        assert by_name["RUL-05 one mock send per sent outbound row"].ok
        assert by_name["XC-02 one agent_runs row per model call"].ok
