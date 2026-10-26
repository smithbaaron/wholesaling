"""Step 8 locked template (A7-09, SEL-05) and the agreement-claim guard (BB-08)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.agents.a7.conftest import IDENTITY
from wholesaling.agents.a5 import prohibited_claim_flags
from wholesaling.agents.a7.templates import (
    REQUIRED_PHRASE,
    check_no_agreement_claim,
    check_offer_body,
    format_money,
    render_offer,
    render_reengage,
    render_reminder,
    seller_footer,
)
from wholesaling.agents.templates.investor_outreach import SenderIdentity


def test_offer_renders_number_terms_days_and_footer() -> None:
    draft = render_offer(
        seller_name="Pat Doe",
        address="1204 Oak Hollow Dr, Houston, TX 77008",
        offer=Decimal(88500),
        cash_close_days=21,
        identity=IDENTITY,
    )
    assert draft.subject == "Cash offer for 1204 Oak Hollow Dr, Houston, TX 77008"
    assert "Hi Pat," in draft.body
    assert "$88,500" in draft.body
    assert REQUIRED_PHRASE in draft.body.lower()
    assert "close with cash in 21 days" in draft.body
    assert "equitable interest" in draft.body  # TX disclosure (SEL-05)
    assert "123 Main St, Houston, TX 77002" in draft.body and "unsubscribe" in draft.body
    assert draft.variables == {
        "seller_name": "Pat",
        "address": "1204 Oak Hollow Dr, Houston, TX 77008",
        "offer": "$88,500",
        "cash_close_days": 21,
    }
    assert check_offer_body(draft.body, offer=Decimal(88500), cash_close_days=21) == []


def test_offer_never_discloses_fee_or_buyer() -> None:
    draft = render_offer(
        seller_name=None, address="X", offer=Decimal(50000), cash_close_days=21, identity=IDENTITY
    )
    lowered = draft.body.lower()
    assert (
        "fee" not in lowered and "buyer" not in lowered and "assign" in lowered
    )  # disclosure only
    assert "Hi there," in draft.body


def test_offer_passes_a5_prohibited_claims_precheck() -> None:
    draft = render_offer(
        seller_name="Pat", address="X", offer=Decimal(50000), cash_close_days=21, identity=IDENTITY
    )
    assert prohibited_claim_flags(draft.body) == [] and prohibited_claim_flags(draft.subject) == []


@pytest.mark.parametrize(
    ("body", "violation"),
    [
        (
            "We offer $90,000. Subject to inspection and signed agreement. Close in 21 days.",
            "offer_number_missing",
        ),
        (
            "We offer $88,500 and can close in 21 days.",
            "missing_subject_to_inspection_and_signed_agreement",
        ),
        (
            "We offer $88,500, subject to inspection and signed agreement.",
            "missing_cash_close_days",
        ),
        (
            "We offer $88,500, subject to inspection and signed agreement, close in 21 days. "
            "Our assignment fee is $10,000.",
            "fee_disclosure",
        ),
        (
            "We offer $88,500, subject to inspection and signed agreement, close in 21 days. "
            "We already have a buyer lined up.",
            "buyer_lined_up",
        ),
        (
            "We offer $88,500, subject to inspection and signed agreement, close in 21 days. "
            "The seller has agreed to sell.",
            "agreement_claim:seller has agreed",
        ),
    ],
)
def test_check_offer_body_catches_each_rule(body: str, violation: str) -> None:
    assert violation in check_offer_body(body, offer=Decimal(88500), cash_close_days=21)


def test_check_offer_body_days_must_match_config() -> None:
    body = "We offer $88,500, subject to inspection and signed agreement; we can close in 30 days."
    assert "missing_cash_close_days" in check_offer_body(
        body, offer=Decimal(88500), cash_close_days=21
    )


def test_reminder_and_reengage_templates() -> None:
    rem = render_reminder(
        seller_name="Pat Doe",
        address="X",
        offer=Decimal(88500),
        sent_on="2026-10-25",
        cash_close_days=21,
        identity=IDENTITY,
    )
    assert (
        "$88,500" in rem.body and "2026-10-25" in rem.body and REQUIRED_PHRASE in rem.body.lower()
    )
    assert check_offer_body(rem.body, offer=Decimal(88500), cash_close_days=21) == []
    re_ = render_reengage(seller_name="Pat", address="X", identity=IDENTITY)
    assert "$" not in re_.body  # no number after expiry: a fresh A7 run is needed
    assert REQUIRED_PHRASE in re_.body.lower() and re_.subject == "Re: X"
    assert prohibited_claim_flags(re_.body) == [] and prohibited_claim_flags(rem.body) == []


def test_seller_footer_requires_postal_address() -> None:
    no_address = SenderIdentity("Aaron", "NorthStack", None, 'reply "unsubscribe"')
    with pytest.raises(ValueError, match="SENDER_POSTAL_ADDRESS"):
        seller_footer(no_address)
    text = seller_footer(IDENTITY)
    assert text.startswith("Disclosure: NorthStack is a real estate investor")
    assert text.endswith('Reply "unsubscribe" to opt out.')


def test_agreement_claim_guard() -> None:
    assert check_no_agreement_claim("The owner has agreed to our terms") == ["owner has agreed"]
    assert check_no_agreement_claim("Property is under contract") == ["under contract"]
    assert check_no_agreement_claim("Candidate only; no contact made") == []
    assert check_no_agreement_claim("A7 offer status accepted; contract requires signature") == []


def test_format_money() -> None:
    assert format_money(Decimal("88500.00")) == "$88,500"
    assert format_money(1234567) == "$1,234,567"
