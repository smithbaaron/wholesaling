"""Seller-facing copy and the compliance blocks every seller message carries (REQUIREMENTS.md
SEL-04, SEL-05, CMP-01, CMP-03, XC-07, PRE-16).

Three fixed pieces are appended, in this order, to **every** message a seller receives, whether
it is an S3 sequence step, an S4 intake reply or a human-edited approval:

1. ``TX_EQUITABLE_INTEREST_DISCLOSURE`` - the Texas equitable-interest disclosure (CMP-03): a
   plain-language statement that we are wholesalers who may assign our equitable interest in a
   purchase contract, and that we are not licensed brokers representing the seller. The wording
   is ``legal_review="pending"`` until counsel signs it off (CMP-04, PRE-16); the structure is
   fixed, the words may change.
2. The CAN-SPAM footer from ``investor_outreach.footer`` (CMP-01): sender identity, postal
   address, opt-out line. Reused as is so one PRE-02 setting feeds both funnels.

``render_seller_message`` assembles them; ``missing_compliance_blocks`` is the check the agents,
the send path and the tests use to assert nothing seller-facing ever goes out without them.

The copy itself (initial + three follow-ups, SEL-04) is ``pending_approval`` until PRE-16. It
is rendered with ``investor_outreach.render`` so the XC-07 denylist applies to every value and
to the result. SMS templates deliberately do not exist: POL-03e is OFF in this build (RUL-06).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from wholesaling.agents.templates.investor_outreach import (
    PENDING_APPROVAL,
    ApprovalStatus,
    ProhibitedClaimError,
    SenderIdentity,
    find_prohibited,
    footer,
    placeholders,
    render,
)

LegalReview = Literal["pending", "approved"]


@dataclass(frozen=True, slots=True)
class SellerTemplate:
    """A seller-facing template. ``legal_review`` tracks the CMP-04 counsel review separately
    from the PRE-16 copy approval: both must be ``approved`` before a live seller send."""

    key: str
    body: str
    approval_status: ApprovalStatus
    source_row: str
    legal_review: LegalReview = "pending"

    @property
    def placeholders(self) -> frozenset[str]:
        return placeholders(self.body)


# --------------------------------------------------------------------------------------------
# SEL-05 / CMP-03: Texas equitable-interest disclosure
# --------------------------------------------------------------------------------------------

TX_EQUITABLE_INTEREST_DISCLOSURE: Final[str] = (
    "Disclosure: {company} is a real estate wholesaler, not a licensed real estate broker or "
    "agent, and does not represent you in this transaction. If we agree on terms, we would sign "
    "a purchase contract with you and may then assign our equitable interest in that contract "
    "to another buyer, who would close the purchase. We may earn a fee from that assignment. "
    "You are free to consult an attorney or a licensed agent before signing anything."
)

DISCLOSURE_TEMPLATE: Final[SellerTemplate] = SellerTemplate(
    key="tx_equitable_interest_disclosure",
    body=TX_EQUITABLE_INTEREST_DISCLOSURE,
    approval_status=PENDING_APPROVAL,
    source_row="SEL-05 / CMP-03",
    legal_review="pending",
)

# --------------------------------------------------------------------------------------------
# SEL-04: initial + 3 follow-ups (3 / 7 / 14 days from config/sequences.yaml)
# --------------------------------------------------------------------------------------------

SELLER_SUBJECTS: Final[tuple[str, ...]] = (
    "Your property at {property_address}",
    "Question about {property_address}",
)

INITIAL_TEMPLATE: Final[SellerTemplate] = SellerTemplate(
    key="seller_initial",
    body=(
        "Hi {owner_first_name},\n"
        "\n"
        "My name is {sender_name} with {company}. We buy houses in the {market} area directly "
        "from owners, as-is, with a cash close and no repairs, showings or agent commissions.\n"
        "\n"
        "I am reaching out about {property_address}. If you have thought about selling it, I "
        "would like to learn a little about the property and make you a written, no-obligation "
        "cash offer. If not, no problem at all - just let me know and I will not follow up "
        "again.\n"
        "\n"
        "Best,\n"
        "{sender_name}"
    ),
    approval_status=PENDING_APPROVAL,
    source_row="SEL-04",
)

FOLLOWUP_TEMPLATES: Final[tuple[SellerTemplate, ...]] = (
    SellerTemplate(
        key="seller_followup_1",
        body=(
            "Hi {owner_first_name},\n"
            "\n"
            "Following up on my note about {property_address}. If selling is something you are "
            "considering, three quick things would let me put a number together: the overall "
            "condition of the house, how soon you would want to close, and whether anyone is "
            "living there now.\n"
            "\n"
            "Best,\n"
            "{sender_name}"
        ),
        approval_status=PENDING_APPROVAL,
        source_row="SEL-04",
    ),
    SellerTemplate(
        key="seller_followup_2",
        body=(
            "Hi {owner_first_name},\n"
            "\n"
            "Checking in once more about {property_address}. We can buy as-is and work around "
            "your timeline, and the offer is in writing with no obligation. A short reply with "
            "what you would want for it is enough to get started.\n"
            "\n"
            "Best,\n"
            "{sender_name}"
        ),
        approval_status=PENDING_APPROVAL,
        source_row="SEL-04",
    ),
    SellerTemplate(
        key="seller_followup_3",
        body=(
            "Hi {owner_first_name},\n"
            "\n"
            "This is my last note about {property_address}. If now is not the right time, I "
            "understand - reply whenever that changes and I will be glad to help.\n"
            "\n"
            "Best,\n"
            "{sender_name}"
        ),
        approval_status=PENDING_APPROVAL,
        source_row="SEL-04",
    ),
)

ALL_TEMPLATES: Final[tuple[SellerTemplate, ...]] = (INITIAL_TEMPLATE, *FOLLOWUP_TEMPLATES)

# Greeting fallback when the owner's first name is unknown (an estate, an LLC, a trust).
FALLBACK_FIRST_NAME: Final[str] = "there"

# --------------------------------------------------------------------------------------------
# Compliance assembly
# --------------------------------------------------------------------------------------------


def disclosure(company: str) -> str:
    """The disclosure block for ``company``. XC-07 checked like any other rendered text."""
    return render(TX_EQUITABLE_INTEREST_DISCLOSURE, company=company)


def seller_compliance_block(sender: SenderIdentity) -> str:
    """Disclosure + CAN-SPAM footer, exactly as appended to every seller message."""
    return disclosure(sender.company) + "\n\n" + footer(sender)


def render_seller_message(
    template: SellerTemplate | str, sender: SenderIdentity, **params: str
) -> str:
    """Render the copy and append the compliance block. ``params`` may include values the
    template does not use (a shorter follow-up names no market); ``sender_name`` and
    ``company`` are filled from ``sender`` when the template asks for them."""
    body = template.body if isinstance(template, SellerTemplate) else template
    needed = placeholders(body)
    available = {**params, "sender_name": sender.name, "company": sender.company}
    chosen = {k: v for k, v in available.items() if k in needed}
    return render(body, **chosen) + "\n\n" + seller_compliance_block(sender)


def missing_compliance_blocks(text: str, sender: SenderIdentity) -> list[str]:
    """Which SEL-05 pieces ``text`` lacks: ``disclosure``, ``identity``, ``postal_address``,
    ``opt_out``. Empty list means the message may be sent."""
    missing: list[str] = []
    if disclosure(sender.company) not in text:
        missing.append("disclosure")
    identity = f"{sender.name.strip()}, {sender.company.strip()}"
    if identity not in text:
        missing.append("identity")
    address = (sender.postal_address or "").strip()
    if not address or address not in text:
        missing.append("postal_address")
    if sender.opt_out_text.strip() not in text:
        missing.append("opt_out")
    return missing


def ensure_compliance_block(text: str, sender: SenderIdentity) -> str:
    """Append the compliance block when any piece is missing (human edits, QA rewrites)."""
    if not missing_compliance_blocks(text, sender):
        return text
    return text.rstrip() + "\n\n" + seller_compliance_block(sender)


def check_no_prohibited(text: str) -> None:
    """Raise ``ProhibitedClaimError`` for XC-07 phrases; used on model-written drafts."""
    hit = find_prohibited(text)
    if hit is not None:
        msg = f"seller message contains prohibited phrase {hit!r} (XC-07)"
        raise ProhibitedClaimError(msg)


__all__ = [
    "ALL_TEMPLATES",
    "DISCLOSURE_TEMPLATE",
    "FALLBACK_FIRST_NAME",
    "FOLLOWUP_TEMPLATES",
    "INITIAL_TEMPLATE",
    "SELLER_SUBJECTS",
    "TX_EQUITABLE_INTEREST_DISCLOSURE",
    "LegalReview",
    "SellerTemplate",
    "check_no_prohibited",
    "disclosure",
    "ensure_compliance_block",
    "missing_compliance_blocks",
    "render_seller_message",
    "seller_compliance_block",
]
