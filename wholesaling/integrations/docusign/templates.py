"""Registry of the two DocuSign templates the disposition path uses (INT-16, PRE-13).

The templates themselves live in the DocuSign account and are drafted/reviewed by a Texas
attorney (PRE-13); this module only knows their *shape*: the recipient roles and the text-tab
labels the PSA and the Assignment Agreement expose, so a deal record can be mapped onto them.
Tab labels are placeholders agreed with the attorney; renaming one here and on the template is a
coordinated change (``docs/integrations/docusign.md``).

Nothing here is specific to the mock or the live gateway, and nothing here sends anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from wholesaling.integrations.docusign.models import (
    EnvelopeRequest,
    EnvelopeStatus,
    TemplateRecipient,
)

TEMPLATE_PSA: Final = "psa"
TEMPLATE_ASSIGNMENT: Final = "assignment"

ROLE_SELLER: Final = "Seller"
ROLE_BUYER: Final = "Buyer"  # us, on the PSA (the assignable purchaser)
ROLE_ASSIGNOR: Final = "Assignor"  # us, on the Assignment Agreement
ROLE_ASSIGNEE: Final = "Assignee"  # the end buyer


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    key: str
    display_name: str
    roles: tuple[str, ...]
    # Text-tab labels the template exposes, in the order they appear on the document.
    tab_labels: tuple[str, ...]
    settings_field: str  # the Settings attribute that holds the DocuSign templateId
    requirement: str


PSA_TEMPLATE: Final[TemplateSpec] = TemplateSpec(
    key=TEMPLATE_PSA,
    display_name="Purchase and Sale Agreement (assignable)",
    roles=(ROLE_SELLER, ROLE_BUYER),
    tab_labels=(
        "property_address",
        "seller_name",
        "buyer_entity",
        "purchase_price",
        "earnest_money_amount",
        "option_fee",
        "option_period_days",
        "inspection_deadline",
        "closing_date",
        "title_company",
        "assignability_clause",  # "Buyer may assign this contract" - fixed text on the template
        "equitable_interest_disclosure",  # CMP-03 TX disclosure - fixed text on the template
    ),
    settings_field="docusign_psa_template_id",
    requirement="INT-16 / SEL-10",
)

ASSIGNMENT_TEMPLATE: Final[TemplateSpec] = TemplateSpec(
    key=TEMPLATE_ASSIGNMENT,
    display_name="Assignment of Purchase and Sale Agreement",
    roles=(ROLE_ASSIGNOR, ROLE_ASSIGNEE),
    tab_labels=(
        "property_address",
        "original_contract_date",
        "seller_name",
        "assignor_entity",
        "assignee_name",
        "assignment_fee",
        "assignee_deposit",
        "deposit_due_date",
        "closing_date",
        "title_company",
        "earnest_money_credit",
    ),
    settings_field="docusign_assignment_template_id",
    requirement="INT-16 / SEL-10",
)

TEMPLATES: Final[dict[str, TemplateSpec]] = {
    PSA_TEMPLATE.key: PSA_TEMPLATE,
    ASSIGNMENT_TEMPLATE.key: ASSIGNMENT_TEMPLATE,
}


class UnknownTemplate(KeyError):
    """``template_key`` is not in the registry."""


def get_template(key: str) -> TemplateSpec:
    try:
        return TEMPLATES[key]
    except KeyError as exc:
        raise UnknownTemplate(key) from exc


def _text(value: Any) -> str:
    """Tab values are strings. Money is rendered with cents, dates as ISO text."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def tabs_from_fields(spec: TemplateSpec, fields: Mapping[str, Any]) -> dict[str, str]:
    """Pick the registry's tab labels out of a deal-record mapping; unknown keys are ignored
    and missing ones are left for the signer (never invented)."""
    out: dict[str, str] = {}
    for label in spec.tab_labels:
        if label in fields and fields[label] is not None and _text(fields[label]) != "":
            out[label] = _text(fields[label])
    return out


def build_psa_request(
    *,
    template_id: str,
    seller_name: str,
    seller_email: str,
    buyer_entity: str,
    buyer_email: str,
    fields: Mapping[str, Any],
    idempotency_key: str,
    status: EnvelopeStatus = EnvelopeStatus.CREATED,
) -> EnvelopeRequest:
    """The PSA envelope: Seller signs first, then our entity. Draft (``created``) by default so
    the human sends it from DocuSign after review (POL-03i)."""
    tabs = tabs_from_fields(
        PSA_TEMPLATE, {**fields, "seller_name": seller_name, "buyer_entity": buyer_entity}
    )
    address = tabs.get("property_address", "the property")
    return EnvelopeRequest(
        template_key=TEMPLATE_PSA,
        template_id=template_id,
        email_subject=f"Purchase and Sale Agreement - {address}",
        recipients=[
            TemplateRecipient(
                role_name=ROLE_SELLER, name=seller_name, email=seller_email, routing_order=1
            ),
            TemplateRecipient(
                role_name=ROLE_BUYER, name=buyer_entity, email=buyer_email, routing_order=2
            ),
        ],
        prefill_tabs=tabs,
        status=status,
        idempotency_key=idempotency_key,
    )


def build_assignment_request(
    *,
    template_id: str,
    assignor_entity: str,
    assignor_email: str,
    assignee_name: str,
    assignee_email: str,
    fields: Mapping[str, Any],
    idempotency_key: str,
    status: EnvelopeStatus = EnvelopeStatus.CREATED,
) -> EnvelopeRequest:
    """The Assignment Agreement: the end buyer (Assignee) signs first, then our entity."""
    tabs = tabs_from_fields(
        ASSIGNMENT_TEMPLATE,
        {**fields, "assignor_entity": assignor_entity, "assignee_name": assignee_name},
    )
    address = tabs.get("property_address", "the property")
    return EnvelopeRequest(
        template_key=TEMPLATE_ASSIGNMENT,
        template_id=template_id,
        email_subject=f"Assignment of Contract - {address}",
        recipients=[
            TemplateRecipient(
                role_name=ROLE_ASSIGNEE, name=assignee_name, email=assignee_email, routing_order=1
            ),
            TemplateRecipient(
                role_name=ROLE_ASSIGNOR, name=assignor_entity, email=assignor_email, routing_order=2
            ),
        ],
        prefill_tabs=tabs,
        status=status,
        idempotency_key=idempotency_key,
    )


__all__ = [
    "ASSIGNMENT_TEMPLATE",
    "PSA_TEMPLATE",
    "ROLE_ASSIGNEE",
    "ROLE_ASSIGNOR",
    "ROLE_BUYER",
    "ROLE_SELLER",
    "TEMPLATES",
    "TEMPLATE_ASSIGNMENT",
    "TEMPLATE_PSA",
    "TemplateSpec",
    "UnknownTemplate",
    "build_assignment_request",
    "build_psa_request",
    "get_template",
    "tabs_from_fields",
]
