"""S3 Seller Outreach and the seller templates: every message carries the TX disclosure, sender
identity, postal address and opt-out (REQUIREMENTS.md SEL-04, SEL-05, CMP-01, CMP-03, XC-07,
POL-03e, RUL-06)."""

from __future__ import annotations

import uuid

import pytest

from wholesaling.agents.a5 import prohibited_claim_flags
from wholesaling.agents.s3 import (
    TOTAL_STEPS,
    DisabledSmsGateway,
    S3SellerOutreach,
    SmsDisabledError,
    SmsGateway,
)
from wholesaling.agents.templates import seller_outreach as t
from wholesaling.agents.templates.investor_outreach import (
    PENDING_APPROVAL,
    ProhibitedClaimError,
    SenderIdentity,
    TemplateRenderError,
)
from wholesaling.config.settings import Settings
from wholesaling.policy.actions import PolicyAction

SENDER = SenderIdentity(
    name="Aaron",
    company="NorthStack",
    postal_address="123 Main St, Houston, TX 77002",
    opt_out_text=(
        'If you would rather not hear from us, reply with "unsubscribe" and we will remove you.'
    ),
)
LEAD_ID = uuid.UUID("00000000-0000-0000-0000-000000000007")


@pytest.fixture
def s3() -> S3SellerOutreach:
    return S3SellerOutreach(SENDER)


# -- disclosure and compliance block (SEL-05) ---------------------------------------------------


def test_disclosure_is_plain_language_and_pending_legal_review() -> None:
    text = t.disclosure("NorthStack")
    for phrase in (
        "wholesaler",
        "not a licensed real estate broker",
        "does not represent you",
        "assign our equitable interest",
        "purchase contract",
        "fee",
    ):
        assert phrase in text
    assert t.DISCLOSURE_TEMPLATE.legal_review == "pending"
    assert t.DISCLOSURE_TEMPLATE.approval_status == PENDING_APPROVAL
    assert t.DISCLOSURE_TEMPLATE.source_row == "SEL-05 / CMP-03"


def test_disclosure_passes_the_xc07_denylists() -> None:
    text = t.disclosure("NorthStack")
    assert prohibited_claim_flags(text) == []  # A5 pre-check
    t.check_no_prohibited(text)  # template denylist


@pytest.mark.parametrize("template", t.ALL_TEMPLATES, ids=lambda x: x.key)
def test_every_seller_template_renders_with_all_four_blocks(template: t.SellerTemplate) -> None:
    body = t.render_seller_message(
        template,
        SENDER,
        owner_first_name="Dolores",
        property_address="1420 Pecan Grove Dr",
        market="Houston",
    )
    assert t.missing_compliance_blocks(body, SENDER) == []
    assert "Aaron, NorthStack" in body and "123 Main St" in body and "unsubscribe" in body
    assert body.index("Disclosure:") < body.index("--\nAaron, NorthStack")
    assert prohibited_claim_flags(body) == []
    assert template.approval_status == PENDING_APPROVAL and template.legal_review == "pending"


def test_missing_compliance_blocks_names_each_missing_piece() -> None:
    assert t.missing_compliance_blocks("Hi there", SENDER) == [
        "disclosure",
        "identity",
        "postal_address",
        "opt_out",
    ]
    partial = "Hi\n\n" + t.disclosure("NorthStack") + "\n\nAaron, NorthStack"
    assert t.missing_compliance_blocks(partial, SENDER) == ["postal_address", "opt_out"]


def test_ensure_compliance_block_appends_only_when_needed() -> None:
    full = t.render_seller_message("Hi {owner_first_name}", SENDER, owner_first_name="Sam")
    assert t.ensure_compliance_block(full, SENDER) == full
    edited = "Human rewrote the whole thing."
    fixed = t.ensure_compliance_block(edited, SENDER)
    assert fixed.startswith(edited) and t.missing_compliance_blocks(fixed, SENDER) == []


def test_render_refuses_prohibited_values_and_unknown_placeholders() -> None:
    with pytest.raises(ProhibitedClaimError):
        t.render_seller_message(
            t.INITIAL_TEMPLATE,
            SENDER,
            owner_first_name="x",
            property_address="our inventory",
            market="H",
        )
    with pytest.raises(TemplateRenderError):
        t.render_seller_message("Hi {owner_first_name}", SENDER)


def test_compliance_block_refuses_without_postal_address() -> None:
    bare = SenderIdentity(
        name="A", company="B", postal_address=None, opt_out_text="reply unsubscribe"
    )
    with pytest.raises(ValueError, match="SENDER_POSTAL_ADDRESS"):
        t.seller_compliance_block(bare)
    with pytest.raises(ValueError):
        S3SellerOutreach(bare)


def test_sender_identity_from_settings_feeds_both_funnels() -> None:
    s = Settings(_env_file=None, app_env="test", sender_postal_address="1 Main St")  # type: ignore[call-arg]
    sender = SenderIdentity.from_settings(s)
    assert t.missing_compliance_blocks(t.render_seller_message("x", sender), sender) == []


# -- S3 drafts (SEL-04) ------------------------------------------------------------------------


def test_sequence_plan_reads_3_7_14_from_config(s3: S3SellerOutreach) -> None:
    plan = s3.sequence_plan()
    assert plan.intervals_days == (3, 7, 14) and plan.total_steps == TOTAL_STEPS == 4


def test_sequence_has_four_steps_in_one_thread_all_with_disclosure(s3: S3SellerOutreach) -> None:
    drafts = s3.sequence(
        seller_lead_id=LEAD_ID,
        contact_email="dolores@example.test",
        property_address="1420 Pecan Grove Dr",
        market="Houston",
        owner_first_name="Dolores",
    )
    assert [d.step for d in drafts] == [0, 1, 2, 3]
    assert drafts[0].thread_strategy == "new_conversation"
    assert all(d.thread_strategy == "reply_in_conversation" for d in drafts[1:])
    assert all(d.policy_action is PolicyAction.SELLER_OUTREACH_EMAIL for d in drafts)
    assert all(t.missing_compliance_blocks(d.body, SENDER) == [] for d in drafts)
    assert all(d.to == "dolores@example.test" for d in drafts)
    assert drafts[0].subject == "Question about 1420 Pecan Grove Dr"  # variant 7 % 2 == 1
    assert all(d.subject == "RE: Question about 1420 Pecan Grove Dr" for d in drafts[1:])
    assert drafts[0].subject_variant == 1 and drafts[1].subject_variant is None
    assert "Hi Dolores," in drafts[0].body and "Houston" in drafts[0].body
    assert drafts[0].legal_review == "pending"


def test_missing_first_name_falls_back_without_inventing_one(s3: S3SellerOutreach) -> None:
    d = s3.draft(
        step=0,
        seller_lead_id=LEAD_ID,
        contact_email="e@x.test",
        property_address="1 A St",
        market="Houston",
        owner_first_name=None,
    )
    assert d.body.startswith("Hi there,")


def test_subject_variant_is_deterministic(s3: S3SellerOutreach) -> None:
    a = uuid.UUID("00000000-0000-0000-0000-000000000002")
    assert (
        S3SellerOutreach.subject_variant(a) == 0 and S3SellerOutreach.subject_variant(LEAD_ID) == 1
    )


def test_bad_step_rejected(s3: S3SellerOutreach) -> None:
    with pytest.raises(ValueError):
        s3.template_for_step(4)


# -- SMS stays off (POL-03e, RUL-06) ---------------------------------------------------------------


async def test_sms_gateway_never_sends() -> None:
    gw = DisabledSmsGateway()
    assert isinstance(gw, SmsGateway) and gw.mode == "off"
    with pytest.raises(SmsDisabledError, match="RUL-06"):
        await gw.send("+17135550101", "hi", consent_ref="none")
    assert gw.attempts == [{"to": "+17135550101", "consent_ref": "none"}]
    assert (await gw.healthcheck()).ok


def test_no_sms_template_exists() -> None:
    assert not [name for name in dir(t) if "SMS" in name.upper() and "TEMPLATE" in name.upper()]
