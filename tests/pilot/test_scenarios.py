"""The scenario catalogue: coverage of the BO-09 list, script consistency, derived expectations."""

from __future__ import annotations

import pytest

from wholesaling.agents.a4 import (
    CLASSIFY_SCHEMA,
    DRAFT_SCHEMA,
    EXTRACT_SCHEMA,
    detect_auto_reply,
    strip_quoted_text,
)
from wholesaling.agents.a5 import QA_SCHEMA, prohibited_claim_flags
from wholesaling.db.models.enums import MessageClassification
from wholesaling.pilot.scenarios import (
    PILOT_MARKETS,
    SCENARIOS,
    SCENARIOS_BY_ID,
    ReplyExpectation,
    Scenario,
    scenarios_for_markets,
)
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

REQUIRED_BEHAVIOURS = {
    "no reply, four touches": lambda s: s.id == "priority_no_reply",
    "interested + full buy box after touch 1": lambda s: s.id == "prospect_interested_full_buy_box",
    "partial buy box then correction": lambda s: s.id == "partial_then_correction",
    "two buy boxes in one email": lambda s: s.id == "two_buy_boxes",
    "question": lambda s: s.id == "question",
    "not-now with date": lambda s: s.id == "not_now_with_date",
    "not interested": lambda s: s.id == "not_interested",
    "opt-out after touch 2": lambda s: s.id == "opt_out_after_touch_2",
    "out-of-office then real reply": lambda s: s.id == "ooo_then_real_reply",
    "sarcasm": lambda s: s.id == "sarcasm",
    "bounce after touch 1": lambda s: s.id == "bounce_after_touch_1",
    "reply quoting old text": lambda s: s.id == "quoted_old_text",
    "priority vs prospect": lambda s: s.id == "priority_before_prospect",
    "suppressed before start": lambda s: s.id == "suppressed_before_start",
    "price/contract talk queues at L1": lambda s: s.id == "l1_auto_send_then_price_talk",
    "L1 auto-send path": lambda s: s.id == "l1_auto_send_then_price_talk",
}


def test_sixteen_named_scenarios_cover_the_bo09_list() -> None:
    assert len(SCENARIOS) == 16
    assert len({s.id for s in SCENARIOS}) == 16
    for behaviour, predicate in REQUIRED_BEHAVIOURS.items():
        assert any(predicate(s) for s in SCENARIOS), behaviour


def test_personas_are_bound_to_fixture_investors_in_the_pilot_markets() -> None:
    personas = [s for s in SCENARIOS if not s.is_cohort_check]
    assert 10 <= len(personas) <= 20
    assert {s.market for s in personas} == set(PILOT_MARKETS)
    assert len({(s.market, s.investor) for s in personas}) == len(personas)
    assert sum(1 for s in personas if s.has_contact) == 14


def test_scenarios_for_markets_filters_personas_and_keeps_cohort_checks() -> None:
    houston = scenarios_for_markets(["Houston"])
    assert all(s.market in (None, "Houston") for s in houston)
    assert any(s.is_cohort_check for s in houston)
    assert len(scenarios_for_markets(PILOT_MARKETS)) == 16


def _scripted_replies() -> list[tuple[Scenario, int]]:
    return [(s, i) for s in SCENARIOS for i in range(len(s.replies))]


@pytest.mark.parametrize(
    ("scenario", "index"), _scripted_replies(), ids=lambda v: getattr(v, "id", v)
)
def test_match_phrase_survives_quote_stripping(scenario: Scenario, index: int) -> None:
    reply = scenario.replies[index]
    stripped = strip_quoted_text(reply.body).text
    assert reply.match in stripped
    assert reply.expect.quoted_text_stripped == strip_quoted_text(reply.body).stripped


@pytest.mark.parametrize(
    ("scenario", "index"), _scripted_replies(), ids=lambda v: getattr(v, "id", v)
)
def test_model_scripts_match_the_agents_schemas(scenario: Scenario, index: int) -> None:
    reply = scenario.replies[index]
    script = reply.model
    if script.classify is not None:
        assert set(script.classify) == set(CLASSIFY_SCHEMA["required"])
        assert script.classify["classification"] in {c.value for c in MessageClassification}
        assert script.classify["classification"] == reply.expect.classification
    if script.extract is not None:
        assert set(script.extract) == set(EXTRACT_SCHEMA["required"])
        for item in script.extract["buy_boxes"]:
            assert set(item) == set(EXTRACT_SCHEMA["properties"]["buy_boxes"]["items"]["required"])
        keys = {item["buy_box_key"] for item in script.extract["buy_boxes"]}
        assert keys == set(reply.expect.buy_box_versions)
    if script.draft is not None:
        assert set(script.draft) == set(DRAFT_SCHEMA["required"])
        # XC-07: a scripted draft must never trip the deterministic A5 denylist.
        assert prohibited_claim_flags(script.draft["body"]) == []
        assert reply.expect.draft
    if script.qa is not None:
        assert set(script.qa) == set(QA_SCHEMA["required"])
        assert script.qa["verdict"] == reply.expect.qa_verdict
        assert script.qa["confidence"] == reply.expect.qa_confidence


def test_auto_reply_persona_is_detected_without_a_model() -> None:
    reply = SCENARIOS_BY_ID["ooo_then_real_reply"].replies[0]
    assert detect_auto_reply(subject="Automatic reply: x", body=reply.body, headers=reply.headers)
    assert reply.model.classify is None and reply.expect.model_calls == 0


def test_bounce_persona_is_an_ndr_from_postmaster_in_a_new_conversation() -> None:
    reply = SCENARIOS_BY_ID["bounce_after_touch_1"].replies[0]
    assert reply.kind == "bounce" and not reply.same_conversation
    assert reply.from_address is not None and reply.from_address.startswith("postmaster@")
    assert reply.expect.suppression_reason == "bounce" and reply.expect.signal == "bounce"


# -- derived expectations --------------------------------------------------------------------------


def test_no_reply_persona_completes_four_queued_touches_at_l0() -> None:
    e = SCENARIOS_BY_ID["priority_no_reply"].expected(AutonomyLevel.L0)
    assert (e.sequence_status, e.cancel_reason, e.touches_sent) == ("completed", None, 4)
    assert e.queued_items == 4 and e.auto_touches == 0 and e.inbound_rows == 0
    assert e.tier == "priority"


def test_no_approvals_parks_the_sequence_on_the_first_step() -> None:
    e = SCENARIOS_BY_ID["priority_no_reply"].expected(AutonomyLevel.L0, approve_everything=False)
    assert (e.sequence_status, e.touches_sent, e.queued_items) == ("active", 0, 1)


def test_correction_persona_expects_version_two_and_two_queued_replies() -> None:
    e = SCENARIOS_BY_ID["partial_then_correction"].expected(AutonomyLevel.L0)
    assert e.buy_box_versions == {"default": 2} and e.corrections == 1
    assert (e.touches_sent, e.inbound_rows, e.reply_drafts, e.replies_sent) == (1, 2, 2, 2)
    assert e.queued_items == 3  # the initial + two replies
    assert e.model_calls == 8 and e.cancel_reason == "reply"


def test_two_boxes_persona_expects_two_keyed_versions() -> None:
    e = SCENARIOS_BY_ID["two_buy_boxes"].expected(AutonomyLevel.L0)
    assert e.buy_box_versions == {"flips": 1, "rentals": 1}


def test_opt_out_persona_expects_two_touches_and_suppression() -> None:
    e = SCENARIOS_BY_ID["opt_out_after_touch_2"].expected(AutonomyLevel.L0)
    assert (e.touches_sent, e.cancel_reason, e.suppression_reason) == (2, "opt_out", "opt_out")
    assert e.reply_drafts == 0 and e.classifications == ("opt_out",)


def test_l1_persona_auto_sends_only_the_clean_reply() -> None:
    s = SCENARIOS_BY_ID["l1_auto_send_then_price_talk"]
    e = s.expected(AutonomyLevel.L0)
    assert e.auto_touches == 1 and e.touches_sent == 1
    assert e.replies_auto_sent == 1 and e.replies_sent == 2 and e.queued_items == 1
    # Without the QA stand-in (Temporal mode) the initial queues like every other.
    e2 = s.expected(AutonomyLevel.L0, qa_standin=False)
    assert e2.auto_touches == 0 and e2.queued_items == 2
    # A scope never lowers a global L2.
    assert s.level_for(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L2) is AutonomyLevel.L2
    assert s.level_for(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L0) is AutonomyLevel.L1
    e3 = s.expected(AutonomyLevel.L2)
    assert e3.replies_auto_sent == 2 and e3.queued_items == 0


def test_global_l1_auto_sends_confident_non_sensitive_replies() -> None:
    maria = SCENARIOS_BY_ID["prospect_interested_full_buy_box"]
    assert maria.expected(AutonomyLevel.L0).replies_auto_sent == 0
    assert maria.expected(AutonomyLevel.L1).replies_auto_sent == 1
    # At L1 the initial still queues (no QA review exists for A3 drafts: GAP-04).
    assert maria.expected(AutonomyLevel.L1).auto_touches == 0


def test_reply_expectation_auto_send_rule() -> None:
    clean = ReplyExpectation(
        classification="question", draft=True, qa_verdict="approve", qa_confidence=0.9
    )
    low = ReplyExpectation(
        classification="question", draft=True, qa_verdict="approve", qa_confidence=0.5
    )
    sensitive = ReplyExpectation(
        classification="question",
        draft=True,
        qa_verdict="approve",
        qa_confidence=0.9,
        sensitive=True,
    )
    assert clean.auto_sends_at(AutonomyLevel.L1) and not clean.auto_sends_at(AutonomyLevel.L0)
    assert not low.auto_sends_at(AutonomyLevel.L1)
    assert not sensitive.auto_sends_at(AutonomyLevel.L1)
    assert sensitive.auto_sends_at(AutonomyLevel.L2)


def test_no_contact_and_suppressed_personas_never_send() -> None:
    e = SCENARIOS_BY_ID["no_contact_found"].expected(AutonomyLevel.L0)
    assert not e.sequence_started and e.touches_sent == 0
    e = SCENARIOS_BY_ID["suppressed_before_start"].expected(AutonomyLevel.L2)
    assert e.sequence_started and e.cancel_reason == "suppressed" and e.touches_sent == 0
    assert e.suppression_reason == "manual"


def test_ooo_scenario_documents_the_known_defect_it_trips() -> None:
    s = SCENARIOS_BY_ID["ooo_then_real_reply"]
    assert "GAP-02" in s.known_defects
    assert "touches_sent" in s.known_defects["GAP-02"]
    e = s.expected(AutonomyLevel.L0)
    assert e.touches_sent == 2 and e.inbound_rows == 2  # what INV-22 requires
