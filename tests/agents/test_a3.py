"""A3 and the locked investor templates (REQUIREMENTS.md CPY-01..04, INV-10, INV-11, XC-07,
CMP-01). No model, no database."""

from __future__ import annotations

import uuid

import pytest

from wholesaling.agents.a3 import FALLBACK_FIRST_NAME, TOTAL_STEPS, A3Outreach, OutreachDraft
from wholesaling.agents.templates import investor_outreach as t
from wholesaling.config.tunables import Tunables
from wholesaling.policy.actions import PolicyAction

# CPY-01 as written in REQUIREMENTS.md section 8, slashes marking line breaks.
CPY_01 = (
    "Hi [First Name], / We're always working on new wholesale real estate opportunities "
    "throughout the Houston area and are looking to connect with investors interested in "
    "off-market deals. / Are you currently purchasing properties in Houston? If so, would you "
    "mind sharing your current buy box so we can send opportunities that match your investment "
    "criteria? / Best, / [Your Name]"
)

SENDER = t.SenderIdentity(
    name="Aaron",
    company="NorthStack",
    postal_address="123 Main St, Houston, TX 77002",
    opt_out_text='Reply "unsubscribe" to opt out.',
)


# -- CPY-01 verbatim -----------------------------------------------------------------------------


def test_initial_body_is_cpy_01_verbatim() -> None:
    expected = (
        CPY_01.replace("[First Name]", "{first_name}")
        .replace("Houston", "{market}")
        .replace("[Your Name]", "{sender_name}")
    )
    # Paragraph breaks: the spec's " / " separators are blank lines except before the sender.
    expected = expected.replace(" / Best, / ", "\n\nBest,\n").replace(" / ", "\n\n")
    assert expected == t.INITIAL_BODY


def test_initial_body_rendered_for_houston_matches_spec_text_character_for_character() -> None:
    rendered = t.render(
        t.INITIAL_TEMPLATE, first_name="Jane", market="Houston", sender_name="Aaron"
    )
    spec = (
        CPY_01.replace("[First Name]", "Jane")
        .replace("[Your Name]", "Aaron")
        .replace(" / Best, / ", "\n\nBest,\n")
        .replace(" / ", "\n\n")
    )
    assert rendered == spec


def test_initial_template_is_approved_and_only_parameterised_by_name_market_sender() -> None:
    assert t.INITIAL_TEMPLATE.approval_status == t.APPROVED
    assert t.INITIAL_TEMPLATE.source_row == "CPY-01"
    assert t.INITIAL_TEMPLATE.placeholders == {"first_name", "market", "sender_name"}


def test_initial_body_line_structure() -> None:
    lines = t.INITIAL_BODY.split("\n")
    assert lines[0] == "Hi {first_name},"
    assert lines[1] == ""
    assert lines[-2] == "Best,"
    assert lines[-1] == "{sender_name}"
    assert t.INITIAL_BODY.count("{market}") == 2  # CPY-02: both "Houston" mentions


# -- subjects (INV-11) ---------------------------------------------------------------------------


def test_three_relevance_based_subject_variants() -> None:
    assert len(t.INITIAL_SUBJECTS) == 3
    for s in t.INITIAL_SUBJECTS:
        assert "{market}" in s
        assert t.placeholders(s) == {"market"}
        assert t.find_prohibited(s) is None
        lowered = s.lower()
        for bad in ("urgent", "last chance", "act now", "re:", "fwd:", "!"):
            assert bad not in lowered, s


# -- follow-ups (CPY-03, CPY-04) -----------------------------------------------------------------


def test_followups_are_three_pending_drafts() -> None:
    assert len(t.FOLLOWUP_BODIES) == 3
    for tpl in t.FOLLOWUP_BODIES:
        assert tpl.approval_status == t.PENDING_APPROVAL
        assert tpl.placeholders <= {"first_name", "market", "sender_name"}
        assert tpl.body.startswith("Hi {first_name},")
        assert tpl.body.endswith("Best,\n{sender_name}")
        assert len(tpl.body) < len(t.INITIAL_BODY), "follow-ups are short"
        assert t.find_prohibited(tpl.body) is None


def test_third_followup_carries_cpy_04_tone_verbatim() -> None:
    final = t.FOLLOWUP_BODIES[-1]
    assert final.source_row == "CPY-04"
    assert (
        "Did we miss the mark, or is now simply not a good time? Let us know when would be "
        "better to reconnect."
    ) in final.body


def test_template_keys_are_unique() -> None:
    keys = [t.INITIAL_TEMPLATE.key, *(f.key for f in t.FOLLOWUP_BODIES)]
    assert len(set(keys)) == len(keys)


# -- render guards -------------------------------------------------------------------------------


def test_render_rejects_unknown_placeholder() -> None:
    with pytest.raises(t.TemplateRenderError, match="unknown placeholder"):
        t.render(
            t.INITIAL_TEMPLATE, first_name="A", market="B", sender_name="C", company="NorthStack"
        )


def test_render_rejects_missing_placeholder() -> None:
    with pytest.raises(t.TemplateRenderError, match="missing"):
        t.render(t.INITIAL_TEMPLATE, first_name="A", market="B")


def test_render_rejects_blank_and_non_string_values() -> None:
    with pytest.raises(t.TemplateRenderError, match="blank"):
        t.render(t.INITIAL_TEMPLATE, first_name="  ", market="B", sender_name="C")
    with pytest.raises(t.TemplateRenderError, match="string"):
        t.render(t.INITIAL_TEMPLATE, first_name=5, market="B", sender_name="C")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "market",
    [
        "Houston (we have 12 properties under contract)",
        "Houston - exclusive deals",
        "Houston inventory",
        "Houston, We HAVE deals",
    ],
)
def test_render_refuses_xc_07_claims_in_parameters(market: str) -> None:
    with pytest.raises(t.ProhibitedClaimError):
        t.render(t.INITIAL_TEMPLATE, first_name="Jane", market=market, sender_name="Aaron")


def test_render_refuses_xc_07_claims_in_rendered_text() -> None:
    with pytest.raises(t.ProhibitedClaimError, match="rendered"):
        t.render("We have {n} homes under contract.", n="3")


def test_render_substitutes_literally_without_nested_formatting() -> None:
    out = t.render("Hi {first_name}", first_name="{market}")
    assert out == "Hi {market}"


def test_placeholders_rejects_attribute_and_index_fields() -> None:
    with pytest.raises(t.TemplateRenderError):
        t.placeholders("{a.b}")
    with pytest.raises(t.TemplateRenderError):
        t.placeholders("{0}")


# -- footer (CMP-01) -----------------------------------------------------------------------------


def test_footer_contains_identity_address_and_opt_out() -> None:
    out = t.footer(SENDER)
    assert "Aaron, NorthStack" in out
    assert "123 Main St, Houston, TX 77002" in out
    assert "unsubscribe" in out


def test_footer_requires_physical_address() -> None:
    with pytest.raises(ValueError, match="SENDER_POSTAL_ADDRESS"):
        t.footer(t.SenderIdentity("A", "B", None, "reply unsubscribe"))
    with pytest.raises(ValueError, match="SENDER_POSTAL_ADDRESS"):
        t.footer(t.SenderIdentity("A", "B", "   ", "reply unsubscribe"))


def test_footer_requires_opt_out_instruction() -> None:
    with pytest.raises(ValueError, match="OPT_OUT_TEXT"):
        t.footer(t.SenderIdentity("A", "B", "1 St", "Thanks for reading"))
    with pytest.raises(ValueError, match="CMP-01"):
        t.footer(t.SenderIdentity("", "B", "1 St", "unsubscribe"))


def test_sender_identity_from_settings(settings) -> None:  # type: ignore[no-untyped-def]
    ident = t.SenderIdentity.from_settings(settings)
    assert ident.name == settings.sender_name
    assert ident.postal_address is None  # PRE-02 not cleared -> footer refuses
    with pytest.raises(ValueError, match="SENDER_POSTAL_ADDRESS"):
        t.footer(ident)


# -- A3 ------------------------------------------------------------------------------------------


@pytest.fixture
def a3() -> A3Outreach:
    return A3Outreach(SENDER, Tunables())


INV = uuid.UUID("12345678-1234-5678-1234-567812345678")


def test_a3_refuses_to_construct_without_footer_prerequisites() -> None:
    with pytest.raises(ValueError, match="SENDER_POSTAL_ADDRESS"):
        A3Outreach(t.SenderIdentity("A", "B", None, "unsubscribe"), Tunables())


def test_a3_makes_no_model_calls() -> None:
    import inspect

    import wholesaling.agents.a3 as mod

    src = inspect.getsource(mod)
    assert "AgentRunner" not in src and "llm" not in src


def test_sequence_plan_from_config_defaults(a3: A3Outreach) -> None:
    plan = a3.sequence_plan()
    assert plan.intervals_days == (4, 4, 4)  # OPN-04
    assert plan.total_steps == 4 == TOTAL_STEPS


def test_sequence_plan_rejects_mismatched_or_non_positive_intervals() -> None:
    bad = Tunables.model_validate({"sequences": {"investor_followup_days": [4, 4]}})
    with pytest.raises(ValueError, match="intervals"):
        A3Outreach(SENDER, bad).sequence_plan()
    zero = Tunables.model_validate({"sequences": {"investor_followup_days": [4, 0, 4]}})
    with pytest.raises(ValueError, match="positive"):
        A3Outreach(SENDER, zero).sequence_plan()


def test_subject_variant_is_deterministic_and_covers_all_variants() -> None:
    assert A3Outreach.subject_variant(INV) == A3Outreach.subject_variant(INV)
    assert A3Outreach.subject_variant(INV) == INV.int % len(t.INITIAL_SUBJECTS)
    seen = {A3Outreach.subject_variant(uuid.UUID(int=i)) for i in range(30)}
    assert seen == set(range(len(t.INITIAL_SUBJECTS)))


def test_step_0_draft_is_the_approved_initial(a3: A3Outreach) -> None:
    d = a3.draft(
        step=0, investor_id=INV, contact_email="jane@acme.com", market="Dallas", first_name="Jane"
    )
    assert isinstance(d, OutreachDraft)
    assert d.to == "jane@acme.com"
    assert d.thread_strategy == "new_conversation"
    assert d.policy_action is PolicyAction.INVESTOR_INITIAL_OUTREACH
    assert d.approval_status == t.APPROVED and d.source_row == "CPY-01"
    assert d.subject_variant == A3Outreach.subject_variant(INV)
    assert d.subject == t.INITIAL_SUBJECTS[d.subject_variant].replace("{market}", "Dallas")
    body, footer_block = d.body.split("\n\n--\n")
    assert body == t.render(
        t.INITIAL_TEMPLATE, first_name="Jane", market="Dallas", sender_name="Aaron"
    )
    assert "Houston" not in body  # CPY-02 (the footer address may name Houston)
    assert "123 Main St" in footer_block and "unsubscribe" in footer_block


def test_followup_drafts_reply_in_conversation(a3: A3Outreach) -> None:
    drafts = a3.sequence(
        investor_id=INV, contact_email="jane@acme.com", market="Dallas", first_name="Jane"
    )
    assert [d.step for d in drafts] == [0, 1, 2, 3]
    for d in drafts[1:]:
        assert d.thread_strategy == "reply_in_conversation"
        assert d.policy_action is PolicyAction.INVESTOR_FOLLOWUP
        assert d.approval_status == t.PENDING_APPROVAL
        assert d.subject_variant is None
        assert d.subject == f"RE: {drafts[0].subject}"
        assert d.body.endswith(t.footer(SENDER))
    assert "Did we miss the mark" in drafts[3].body


def test_missing_first_name_falls_back_to_there(a3: A3Outreach) -> None:
    d = a3.draft(step=0, investor_id=INV, contact_email="x@y.com", market="Dallas", first_name=None)
    assert d.body.startswith(f"Hi {FALLBACK_FIRST_NAME},")
    d2 = a3.draft(
        step=0, investor_id=INV, contact_email="x@y.com", market="Dallas", first_name="   "
    )
    assert d2.body.startswith(f"Hi {FALLBACK_FIRST_NAME},")


def test_draft_rejects_out_of_range_step(a3: A3Outreach) -> None:
    with pytest.raises(ValueError, match="step"):
        a3.draft(step=4, investor_id=INV, contact_email="x@y.com", market="Dallas", first_name="J")


def test_draft_refuses_market_with_prohibited_claim(a3: A3Outreach) -> None:
    with pytest.raises(t.ProhibitedClaimError):
        a3.draft(
            step=0,
            investor_id=INV,
            contact_email="x@y.com",
            market="Dallas inventory",
            first_name="J",
        )
