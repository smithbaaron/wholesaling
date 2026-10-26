"""The seller catalogue (BO-14): coverage of the required list, script consistency against the
S4 / A5 schemas and the locked templates, level-dependent expectations."""

from __future__ import annotations

from typing import Any

from wholesaling.agents.a5 import QA_SCHEMA
from wholesaling.agents.a7.templates import OFFER_BODY, REENGAGE_BODY, REMINDER_BODY
from wholesaling.agents.s4 import (
    CLASSIFY_SCHEMA,
    DRAFT_SCHEMA,
    EXTRACT_SCHEMA,
    MAX_ASKS_PER_MESSAGE,
    contains_sensitive_content,
)
from wholesaling.agents.templates.disposition import BLAST_TEMPLATE
from wholesaling.agents.templates.seller_outreach import ALL_TEMPLATES
from wholesaling.api.routes.sell import CONDITION_LABELS, TIMELINE_LABELS
from wholesaling.db.repositories.sellers import ASKED_INTAKE_FIELDS, REQUIRED_INTAKE_FIELDS
from wholesaling.integrations.data.property import PropertyAddress, mock_property_provider
from wholesaling.pilot.seller_scenarios import (
    PERSONA_SCENARIOS,
    SCENARIO_IDS,
    SELLER_LEVEL_ACTIONS,
    SELLER_SCENARIOS,
    SELLER_TEMPLATE_QA,
    SellerMessage,
    SellerScenario,
    scenario,
)
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

REQUIRED_BEHAVIOURS: dict[str, str] = {
    "full-info web form -> offer -> accept -> contract -> blast -> buyer -> deposit -> close": (
        "web_form_full_to_close"
    ),
    "partial email intake over two replies -> offer": "s1_partial_intake_to_dead_deal",
    "what's your offer? (sensitive -> human)": "seed_asks_for_a_number",
    "hostile / opt-out": "seed_hostile_opt_out",
    "no reply (3 follow-ups then nothing)": "seed_no_reply",
    "counter inside the room (L1 auto-accept)": "s1_full_reply_counter_inside_room",
    "counter above MAO (human)": "s1_counter_above_mao_human_declines",
    "offer expires at 14 d, re-engage at 60 d": "s1_offer_expires_then_reengage",
    "3-way source conflict -> human (Low)": "web_form_three_way_conflict",
    "dirty comps -> V3 / V4": "web_form_dirty_comps",
    "AVM gap -> V2 loop": "web_form_avm_gap",
    "pre-send drift -> V6 recompute": "web_form_pre_send_drift",
    "V5 mismatch injected": "web_form_v5_mismatch",
    "deal dead after inspection": "s1_partial_intake_to_dead_deal",
    "market not Active (S1 refuses)": "market_not_active_refused",
    "DNC-listed lead (email proceeds)": "s1_full_reply_counter_inside_room",
    "two leads same parcel (dedupe)": "web_form_same_parcel_merges",
    "photos missing -> confidence drop": "web_form_photos_missing",
    "probate without authority -> gate fails": "web_form_probate_without_authority",
}


def test_the_catalogue_covers_every_required_behaviour() -> None:
    assert len(SELLER_SCENARIOS) == 17
    assert len(set(SCENARIO_IDS)) == len(SCENARIO_IDS)
    assert len(PERSONA_SCENARIOS) >= 14
    for behaviour, scenario_id in REQUIRED_BEHAVIOURS.items():
        assert scenario(scenario_id) in SELLER_SCENARIOS, behaviour
    assert scenario("market_not_active_refused").is_cohort_check


def test_personas_have_one_lead_each_and_the_sources_line_up() -> None:
    owners = [(s.owner, s.address_line1) for s in PERSONA_SCENARIOS]
    # The dedupe persona deliberately reuses Ramirez's lead; every other persona is distinct.
    assert len(set(owners)) == len(owners) - 1
    for s in PERSONA_SCENARIOS:
        assert s.owner and s.address_line1 and s.postal_code
        if s.source == "web_form":
            assert s.form is not None and s.seed is None
            assert s.form.address_line1 == s.address_line1
            assert s.form.postal_code == s.postal_code
            assert s.form.email.endswith("@example.test")
        elif s.source == "seed":
            assert s.seed is not None and s.form is None
            assert s.seed.owner_name == s.owner and s.seed.address_line1 == s.address_line1
        else:
            assert s.source == "s1" and s.seed is None
    assert sum(1 for s in PERSONA_SCENARIOS if s.source == "seed") == 3  # GAP-S01
    assert sum(1 for s in PERSONA_SCENARIOS if s.source == "s1") == 4


def test_web_form_values_are_what_the_route_accepts() -> None:
    for s in PERSONA_SCENARIOS:
        if s.form is None:
            continue
        assert s.form.condition_class in ("", *CONDITION_LABELS)
        assert s.form.timeline in ("", *TIMELINE_LABELS)
        assert s.form.state == "TX" and s.form.city == "Houston"
        assert 0 <= s.form.photos <= 10


def test_property_aliases_point_at_fixture_subjects() -> None:
    provider = mock_property_provider()
    for s in PERSONA_SCENARIOS:
        if s.alias is None:
            continue
        fixture = PropertyAddress(
            s.alias.fixture_line1, "Houston", "TX", s.alias.fixture_postal_code
        )
        assert provider.subject(fixture) is not None, s.id
        # An aliased persona must not sit on a fixture address itself (one subject per address).
        own = PropertyAddress(s.address_line1 or "", "Houston", "TX", s.postal_code)
        assert provider.subject(own) is None, s.id


def _messages() -> list[tuple[SellerScenario, int, SellerMessage]]:
    return [(s, i, m) for s in PERSONA_SCENARIOS for i, m in enumerate(s.messages)]


def _conforms(parsed: dict[str, Any], schema: dict[str, Any]) -> None:
    """Keys exactly the schema's required set; enum members where the schema names them."""
    assert set(parsed) == set(schema["required"])
    for key, prop in schema["properties"].items():
        value = parsed[key]
        options = [prop, *prop.get("anyOf", [])]
        enum = next((o["enum"] for o in options if "enum" in o), None)
        nullable = any(o.get("type") == "null" for o in options)
        if enum is not None and not (value is None and nullable):
            assert value in enum, (key, value)
        if prop.get("type") == "array":
            assert isinstance(value, list), key
        if prop.get("type") == "boolean":
            assert isinstance(value, bool), key


def test_scripted_model_outputs_validate_against_the_s4_and_a5_schemas() -> None:
    for s, _i, m in _messages():
        assert m.match in m.body, (s.id, m.match)
        script = m.model
        if script.classify is not None:
            _conforms(script.classify, CLASSIFY_SCHEMA)
        if script.extract is not None:
            _conforms(script.extract, EXTRACT_SCHEMA)
        if script.draft is not None:
            _conforms(script.draft, DRAFT_SCHEMA)
            assert len(script.draft["asks_for"]) <= MAX_ASKS_PER_MESSAGE
            assert set(script.draft["asks_for"]) <= set(REQUIRED_INTAKE_FIELDS) | set(
                ASKED_INTAKE_FIELDS
            )
            # No dollar figure in any scripted S4 draft: sensitivity comes from asks_offer.
            assert not contains_sensitive_content(script.draft["body"]), s.id
            assert script.qa is not None, s.id
        if script.qa is not None:
            _conforms(script.qa, QA_SCHEMA)
    for verdict in SELLER_TEMPLATE_QA.values():
        _conforms(verdict, QA_SCHEMA)


def test_template_qa_needles_appear_in_the_locked_templates() -> None:
    bodies = [t.body for t in ALL_TEMPLATES] + [
        OFFER_BODY,
        REMINDER_BODY,
        REENGAGE_BODY,
        BLAST_TEMPLATE.body,
    ]
    for needle in SELLER_TEMPLATE_QA:
        assert any(needle in body for body in bodies), needle


def test_messages_triggered_by_our_replies_or_the_offer_are_scripted_consistently() -> None:
    for s, _i, m in _messages():
        assert m.trigger in ("touch", "s4_reply", "offer_sent")
        assert m.delay_hours > 0
        if m.negotiation is not None:
            assert m.trigger == "offer_sent", s.id
            move = m.negotiation
            if move.kind == "counter":
                assert move.human_override is not None, s.id
    # Only the deal-plan personas run to a contract; both accept the offer.
    for s in PERSONA_SCENARIOS:
        if s.deal_plan is not None:
            assert any(
                m.negotiation is not None and m.negotiation.kind == "accept" for m in s.messages
            ), s.id


def test_expectations_differ_by_level_only_where_autonomy_matters() -> None:
    for s in SELLER_SCENARIOS:
        l0, l1 = s.expected(AutonomyLevel.L0), s.expected(AutonomyLevel.L1)
        assert set(l0) == set(l1), s.id
        changed = {k for k in l0 if l0[k] != l1[k]}
        assert changed <= {
            "s4_replies_auto",
            "offer_auto",
            "blast_auto",
            "auto_touches",
            "counter_decision",
            "accepted_by",
            "reminders_queued",
        }, (s.id, changed)
        for gap, names in s.known_defects.items():
            assert gap.startswith("GAP-S")
            assert set(names) <= set(l0), (s.id, gap)


def test_known_defects_name_the_documented_gaps() -> None:
    gaps = {g for s in SELLER_SCENARIOS for g in s.known_defects}
    assert gaps == {"GAP-S03", "GAP-S05", "GAP-S08", "GAP-S09", "GAP-S11"}
    # The happy-path full close is known to fail only on the web-form gate and the lead status.
    full = scenario("web_form_full_to_close")
    assert full.known_defects == {"GAP-S03": ("gates_failed",), "GAP-S11": ("lead_status",)}


def test_seller_level_actions_are_the_five_policy_rows_the_pilot_promotes() -> None:
    assert set(SELLER_LEVEL_ACTIONS) == {
        PolicyAction.SELLER_OUTREACH_EMAIL,
        PolicyAction.SELLER_INTAKE_REPLY,
        PolicyAction.OFFER_SEND,
        PolicyAction.COUNTER_ACCEPTANCE,
        PolicyAction.BUYER_DISPOSITION_BLAST,
    }
    assert PolicyAction.SELLER_OUTREACH_SMS not in SELLER_LEVEL_ACTIONS  # RUL-06: never on
