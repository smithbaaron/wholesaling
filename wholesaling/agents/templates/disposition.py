"""S6 buyer-blast copy (REQUIREMENTS.md SEL-10, BB-09, XC-07, CMP-01, CMP-03).

One template, rendered only for a contract whose row is ``signed``. What it may and may not say:

- It may say the property is **under contract** (the records support it: a human recorded the
  signature, ``contract.signed`` is in the audit log). It must not state or imply that the
  seller has agreed to anything beyond the terms of that contract, that the property is
  available to a particular buyer, or that anything is held for anyone.
- No "guaranteed", no exclusivity, no urgency tricks (``DISPOSITION_PROHIBITED``), and nothing
  invented: every number comes from the offer record and the evidence summary names its source
  and its uncertainty (BB-09 "evidence, reason for match, uncertainty").
- Texas equitable-interest disclosure in the body (CMP-03): we hold an equitable interest under
  an assignable purchase agreement and are not the owner.
- CAN-SPAM footer from ``investor_outreach.footer`` (identity, postal address, opt-out).

The ``investor_outreach.render`` guard is deliberately *not* reused: its denylist forbids
"under contract" because an investor-outreach draft has no deal behind it. Here the phrase is
required (the row is signed) and the guard is the stricter list below, applied to every value and
to the rendered result.

Nothing here calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from wholesaling.agents.templates.investor_outreach import (
    PENDING_APPROVAL,
    ProhibitedClaimError,
    SenderIdentity,
    Template,
    TemplateRenderError,
    footer,
    placeholders,
)

# Pending PRE-16-style copy approval like the follow-ups; the operator sees the full body in the
# review queue at L0 (POL-03k "Draft") before anything goes out.
BLAST_TEMPLATE: Final[Template] = Template(
    key="disposition_blast",
    body=(
        "Hi {first_name},\n"
        "\n"
        "We have a property under contract in {market} that may fit your buy box:\n"
        "\n"
        "{property_address}\n"
        "{deal_summary}\n"
        "\n"
        "What we know and how sure we are:\n"
        "{evidence_summary}\n"
        "\n"
        "We hold an equitable interest in this property under an assignable purchase agreement; "
        "we are not the owner. The seller has agreed only to the terms of that agreement, which "
        "carries its own contingencies and deadlines. We are offering our position by "
        "assignment. Nothing is held for anyone: a buyer moves forward once proof of funds and "
        "deposit terms are in place.\n"
        "\n"
        "If this fits, reply with proof of funds and your earliest closing date. Walkthroughs "
        "are by appointment.\n"
        "\n"
        "Best,\n"
        "{sender_name}"
    ),
    approval_status=PENDING_APPROVAL,
    source_row="SEL-10",
)

BLAST_SUBJECT: Final[str] = "Under contract in {market}: {property_short} (assignable)"

# Phrases that would assert more than the records support. Checked case-insensitively on
# whitespace-normalised text in every parameter and in the rendered body and subject.
# Note the template's own sentence "The seller has agreed only to the terms of that agreement"
# is the disclosure, and is excluded by the ``_ALLOWED`` carve-out below.
DISPOSITION_PROHIBITED: Final[tuple[str, ...]] = (
    "seller agreed to",
    "seller has agreed to",
    "seller accepted",
    "seller has accepted",
    "seller will accept",
    "seller is willing",
    "seller is motivated to accept",
    "owner agreed",
    "guaranteed",
    "guarantee",
    "exclusive",
    "exclusively",
    "first right",
    "held for you",
    "reserved for you",
    "we own",
    "our property",
    "we are the owner",
    "act now",
    "limited time",
    "won't last",
    "will not last",
    "no risk",
)

# Exact sentence from the template that *is* the disclosure.
_ALLOWED: Final[tuple[str, ...]] = ("the seller has agreed only to the terms of that agreement",)

_WS_RE = re.compile(r"\s+")


def find_disposition_prohibited(text: str) -> str | None:
    """First prohibited phrase in ``text``, after removing the template's own disclosure."""
    norm = _WS_RE.sub(" ", text).lower()
    for allowed in _ALLOWED:
        norm = norm.replace(allowed, " ")
    for phrase in DISPOSITION_PROHIBITED:
        if phrase in norm:
            return phrase
    return None


class ContractNotSigned(ValueError):
    """The blast template renders only for a signed contract row (BB-09: only approved deals
    may be described to buyers as available)."""


@dataclass(frozen=True, slots=True)
class DispositionCopy:
    subject: str
    body: str  # includes the CAN-SPAM footer
    template_key: str
    approval_status: str


def _render(body: str, **params: str) -> str:
    needed = placeholders(body)
    given = frozenset(params)
    if unknown := given - needed:
        raise TemplateRenderError(f"unknown placeholder(s) {sorted(unknown)}")
    if missing := needed - given:
        raise TemplateRenderError(f"missing value(s) for {sorted(missing)}")
    for name, value in params.items():
        if not isinstance(value, str) or not value.strip():
            raise TemplateRenderError(f"placeholder {name!r} is blank")
        if (hit := find_disposition_prohibited(value)) is not None:
            raise ProhibitedClaimError(
                f"value for {name!r} contains prohibited phrase {hit!r} (XC-07 / BB-09)"
            )
    out = body
    for name, value in params.items():
        out = out.replace("{" + name + "}", value)
    if (hit := find_disposition_prohibited(out)) is not None:
        raise ProhibitedClaimError(f"rendered text contains prohibited phrase {hit!r}")
    return out


def render_blast(
    *,
    contract_signed: bool,
    first_name: str | None,
    market: str,
    property_address: str,
    property_short: str,
    deal_summary: str,
    evidence_summary: str,
    sender: SenderIdentity,
) -> DispositionCopy:
    """Subject + body (+ footer) for one buyer. Refuses unless the contract row is signed."""
    if not contract_signed:
        raise ContractNotSigned(
            "the disposition blast renders only for a signed contract (BB-09, SEL-11)"
        )
    greeting = (first_name or "").strip() or "there"
    body = _render(
        BLAST_TEMPLATE.body,
        first_name=greeting,
        market=market,
        property_address=property_address,
        deal_summary=deal_summary,
        evidence_summary=evidence_summary,
        sender_name=sender.name,
    )
    subject = _render(BLAST_SUBJECT, market=market, property_short=property_short)
    return DispositionCopy(
        subject=subject,
        body=body.rstrip() + "\n\n" + footer(sender),
        template_key=BLAST_TEMPLATE.key,
        approval_status=BLAST_TEMPLATE.approval_status,
    )


__all__ = [
    "BLAST_SUBJECT",
    "BLAST_TEMPLATE",
    "DISPOSITION_PROHIBITED",
    "ContractNotSigned",
    "DispositionCopy",
    "find_disposition_prohibited",
    "render_blast",
]
