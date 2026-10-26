"""Locked seller-facing templates for the offer path (REQUIREMENTS.md A7-09, SEL-05, SEL-09).

The offer email is a template, not model output: the only variables are the seller's name, the
address, the offer number and the cash-close days from config (OPN-14). It must:

- state the number;
- say the offer is "subject to inspection and signed agreement";
- state a cash close in N days;
- never disclose the assignment fee or spread;
- never claim a buyer is lined up.

``check_offer_body`` enforces those five rules deterministically (it runs before A5 QA and again
in the offer activity); ``check_no_agreement_claim`` is the guard D2 and S5 share so no text ever
says a seller has agreed to sell (BB-08).

Every seller message carries the Texas equitable-interest disclosure, identity, postal address
and an opt-out line (SEL-05); ``seller_footer`` refuses to render without the address, like the
investor footer (CMP-01, PRE-02).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from wholesaling.agents.templates.investor_outreach import SenderIdentity, footer

TEMPLATE_VERSION = "a7-offer-2026-10-25.1"

OFFER_SUBJECT = "Cash offer for {address}"

OFFER_BODY = (
    "Hi {seller_name},\n\n"
    "Thank you for the details on {address}. Based on what you shared and recent sales nearby, "
    "we can offer {offer} in cash for the property in its current condition.\n\n"
    "This offer is subject to inspection and signed agreement. We can close with cash in "
    "{cash_close_days} days of a signed agreement, on a date that works for you, and you would "
    "not pay agent commissions.\n\n"
    "If that works, reply to this email and we will send over a simple purchase agreement for "
    "your review. If the number does not work for you, tell us what would and we will take "
    "another look.\n\n"
    "{footer}"
)

REMINDER_SUBJECT = "Re: Cash offer for {address}"
REMINDER_BODY = (
    "Hi {seller_name},\n\n"
    "Following up on the cash offer of {offer} we sent for {address} on {sent_on}. It is still "
    "subject to inspection and signed agreement, and we can still close in {cash_close_days} "
    "days.\n\n"
    "If you have questions or want to talk through the number, reply here any time.\n\n"
    "{footer}"
)

REENGAGE_SUBJECT = "Re: {address}"
REENGAGE_BODY = (
    "Hi {seller_name},\n\n"
    "We reached out a while back about {address}. Our earlier offer has lapsed, but if selling "
    "is still on your mind we would be glad to take a fresh look and send an updated cash offer, "
    "subject to inspection and signed agreement.\n\n"
    "Reply here if you would like us to.\n\n"
    "{footer}"
)

REQUIRED_PHRASE = "subject to inspection and signed agreement"

# Fee / spread disclosure and buyer-lined-up claims (A7-09). Whole-phrase, case-insensitive.
PROHIBITED_OFFER_PHRASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "fee_disclosure",
        re.compile(r"\b(assignment|wholesale|our|finder'?s?|a)\s+fee\b|\bfee\b", re.I),
    ),
    ("spread_disclosure", re.compile(r"\b(our|the)\s+(spread|margin|profit|markup)\b", re.I)),
    (
        "buyer_lined_up",
        re.compile(
            r"\b(buyer|investor|partner)s?\s+(lined up|ready|waiting|in place|standing by)\b"
            r"|\bwe (already )?have (a|an|the) (buyer|investor|end buyer)\b"
            r"|\balready (sold|spoken for|committed)\b|\bassign(ed|ing)? (it|the contract) to\b",
            re.I,
        ),
    ),
)

# Phrases that would imply a seller has agreed to sell (BB-08, D2 / S5 guard).
AGREEMENT_CLAIM = re.compile(
    r"\b(seller|owner)s?\s+(has|have|had)\s+agreed\b|\bagreed to sell\b|\bunder contract\b"
    r"|\baccepted our offer\b|\bdeal is done\b|\bsecured the property\b",
    re.I,
)

TX_DISCLOSURE = (
    "Disclosure: {company} is a real estate investor, not a licensed broker acting on your "
    "behalf. We may hold an equitable interest in a purchase contract on your property and may "
    "assign that interest to another party. You are encouraged to seek independent advice."
)


def format_money(value: Decimal | int) -> str:
    return f"${int(Decimal(value)):,}"


def seller_footer(identity: SenderIdentity) -> str:
    """SEL-05: TX equitable-interest disclosure + identity + address + opt-out."""
    base = footer(identity)
    return f"{TX_DISCLOSURE.format(company=identity.company.strip())}\n\n{base}"


@dataclass(frozen=True, slots=True)
class RenderedDraft:
    subject: str
    body: str
    template_version: str
    variables: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "template_version": self.template_version,
            "variables": dict(self.variables),
        }


def _name(seller_name: str | None) -> str:
    name = (seller_name or "").strip()
    return name.split()[0] if name else "there"


def render_offer(
    *,
    seller_name: str | None,
    address: str,
    offer: Decimal,
    cash_close_days: int,
    identity: SenderIdentity,
) -> RenderedDraft:
    money = format_money(offer)
    variables = {
        "seller_name": _name(seller_name),
        "address": address,
        "offer": money,
        "cash_close_days": cash_close_days,
    }
    body = OFFER_BODY.format(**variables, footer=seller_footer(identity))
    return RenderedDraft(
        subject=OFFER_SUBJECT.format(address=address),
        body=body,
        template_version=TEMPLATE_VERSION,
        variables=variables,
    )


def render_reminder(
    *,
    seller_name: str | None,
    address: str,
    offer: Decimal,
    sent_on: str,
    cash_close_days: int,
    identity: SenderIdentity,
) -> RenderedDraft:
    variables = {
        "seller_name": _name(seller_name),
        "address": address,
        "offer": format_money(offer),
        "sent_on": sent_on,
        "cash_close_days": cash_close_days,
    }
    return RenderedDraft(
        subject=REMINDER_SUBJECT.format(address=address),
        body=REMINDER_BODY.format(**variables, footer=seller_footer(identity)),
        template_version=TEMPLATE_VERSION,
        variables=variables,
    )


def render_reengage(
    *, seller_name: str | None, address: str, identity: SenderIdentity
) -> RenderedDraft:
    variables = {"seller_name": _name(seller_name), "address": address}
    return RenderedDraft(
        subject=REENGAGE_SUBJECT.format(address=address),
        body=REENGAGE_BODY.format(**variables, footer=seller_footer(identity)),
        template_version=TEMPLATE_VERSION,
        variables=variables,
    )


def check_no_agreement_claim(text: str) -> list[str]:
    """BB-08 / SEL-11 guard: the phrases that would state a seller has agreed."""
    return [m.group(0) for m in AGREEMENT_CLAIM.finditer(text)]


def check_offer_body(body: str, *, offer: Decimal, cash_close_days: int) -> list[str]:
    """A7-09 template guard. Returns the list of violations (empty = compliant)."""
    problems: list[str] = []
    lowered = body.lower()
    if format_money(offer) not in body:
        problems.append("offer_number_missing")
    if REQUIRED_PHRASE not in lowered:
        problems.append("missing_subject_to_inspection_and_signed_agreement")
    if not re.search(rf"\bclose[^.]*\b{cash_close_days}\s+days\b", lowered):
        problems.append("missing_cash_close_days")
    for name, rx in PROHIBITED_OFFER_PHRASES:
        if rx.search(body):
            problems.append(name)
    problems.extend(f"agreement_claim:{m}" for m in check_no_agreement_claim(body))
    return problems


__all__ = [
    "AGREEMENT_CLAIM",
    "OFFER_BODY",
    "OFFER_SUBJECT",
    "PROHIBITED_OFFER_PHRASES",
    "REENGAGE_BODY",
    "REENGAGE_SUBJECT",
    "REMINDER_BODY",
    "REMINDER_SUBJECT",
    "REQUIRED_PHRASE",
    "TEMPLATE_VERSION",
    "TX_DISCLOSURE",
    "RenderedDraft",
    "check_no_agreement_claim",
    "check_offer_body",
    "format_money",
    "render_offer",
    "render_reengage",
    "render_reminder",
    "seller_footer",
]
