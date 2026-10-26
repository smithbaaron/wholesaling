"""DocuSign data shapes: envelope payload, template registry, Connect event + HMAC (INT-16)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from wholesaling.integrations.docusign import (
    ASSIGNMENT_TEMPLATE,
    PSA_TEMPLATE,
    TEMPLATES,
    ConnectEvent,
    EnvelopeRequest,
    EnvelopeStatus,
    TemplateRecipient,
    build_assignment_request,
    build_psa_request,
    connect_signature,
    get_template,
    verify_connect_signature,
)
from wholesaling.integrations.docusign.models import ENVELOPE_TRANSITIONS
from wholesaling.integrations.docusign.templates import UnknownTemplate, tabs_from_fields


def _request(**over: object) -> EnvelopeRequest:
    base: dict[str, object] = {
        "template_key": "psa",
        "template_id": "tpl-1",
        "email_subject": "PSA - 1 Main St",
        "recipients": [
            TemplateRecipient(role_name="Seller", name="Maria", email="m@x.com", tabs={"a": "1"}),
            TemplateRecipient(
                role_name="Buyer", name="NorthStack", email="d@x.com", routing_order=2
            ),
        ],
        "prefill_tabs": {"property_address": "1 Main St"},
        "idempotency_key": "psa:abc",
    }
    base.update(over)
    return EnvelopeRequest(**base)  # type: ignore[arg-type]


def test_payload_follows_esignature_v21_shape() -> None:
    payload = _request().to_api_payload()
    assert payload["templateId"] == "tpl-1"
    assert payload["status"] == "created"  # default: a draft the human sends (POL-03i)
    assert payload["emailSubject"] == "PSA - 1 Main St"
    roles = payload["templateRoles"]
    assert roles[0] == {
        "roleName": "Seller",
        "name": "Maria",
        "email": "m@x.com",
        "routingOrder": "1",
        "tabs": {"textTabs": [{"tabLabel": "a", "value": "1"}]},
    }
    assert roles[1]["routingOrder"] == "2" and "tabs" not in roles[1]
    assert payload["prefillTabs"] == {
        "textTabs": [{"tabLabel": "property_address", "value": "1 Main St"}]
    }
    assert payload["customFields"]["textCustomFields"][0]["value"] == "psa:abc"
    json.dumps(payload)  # JSON-serialisable


def test_payload_sent_status_and_blurb() -> None:
    payload = _request(status=EnvelopeStatus.SENT, email_blurb="Please sign").to_api_payload()
    assert payload["status"] == "sent" and payload["emailBlurb"] == "Please sign"


def test_request_needs_a_recipient() -> None:
    with pytest.raises(ValidationError):
        _request(recipients=[])


def test_status_helpers_and_transitions() -> None:
    assert EnvelopeStatus.COMPLETED.is_terminal and not EnvelopeStatus.SENT.is_terminal
    assert EnvelopeStatus.SENT.is_in_process and EnvelopeStatus.DELIVERED.is_in_process
    assert ENVELOPE_TRANSITIONS[EnvelopeStatus.COMPLETED] == frozenset()
    assert EnvelopeStatus.VOIDED in ENVELOPE_TRANSITIONS[EnvelopeStatus.SENT]
    assert EnvelopeStatus.COMPLETED not in ENVELOPE_TRANSITIONS[EnvelopeStatus.CREATED]


# -- templates registry ---------------------------------------------------------------------------


def test_registry_has_psa_and_assignment_with_roles_and_tabs() -> None:
    assert set(TEMPLATES) == {"psa", "assignment"}
    assert PSA_TEMPLATE.roles == ("Seller", "Buyer")
    assert ASSIGNMENT_TEMPLATE.roles == ("Assignor", "Assignee")
    assert "equitable_interest_disclosure" in PSA_TEMPLATE.tab_labels  # CMP-03
    assert "assignability_clause" in PSA_TEMPLATE.tab_labels
    assert "assignment_fee" in ASSIGNMENT_TEMPLATE.tab_labels
    assert get_template("psa") is PSA_TEMPLATE
    with pytest.raises(UnknownTemplate):
        get_template("lease")


def test_tabs_from_fields_picks_known_labels_only_and_formats_values() -> None:
    tabs = tabs_from_fields(
        PSA_TEMPLATE,
        {
            "property_address": "1 Main St",
            "purchase_price": Decimal("185000"),
            "closing_date": datetime(2026, 12, 1, tzinfo=UTC),
            "unknown": "x",
            "title_company": None,
        },
    )
    assert tabs == {
        "property_address": "1 Main St",
        "purchase_price": "185,000.00",
        "closing_date": "2026-12-01T00:00:00+00:00",
    }


def test_build_psa_request_seller_signs_first_and_is_a_draft() -> None:
    req = build_psa_request(
        template_id="tpl-psa",
        seller_name="Maria Lopez",
        seller_email="maria@example.com",
        buyer_entity="NorthStack LLC",
        buyer_email="deals@example.test",
        fields={"property_address": "1 Main St, Houston, TX", "purchase_price": Decimal("185000")},
        idempotency_key="psa:1",
    )
    assert req.status is EnvelopeStatus.CREATED
    assert [r.role_name for r in req.recipients] == ["Seller", "Buyer"]
    assert [r.routing_order for r in req.recipients] == [1, 2]
    assert req.prefill_tabs["seller_name"] == "Maria Lopez"
    assert req.prefill_tabs["buyer_entity"] == "NorthStack LLC"
    assert "1 Main St" in req.email_subject


def test_build_assignment_request_assignee_signs_first() -> None:
    req = build_assignment_request(
        template_id="tpl-asg",
        assignor_entity="NorthStack LLC",
        assignor_email="deals@example.test",
        assignee_name="Lone Star Holdings",
        assignee_email="buyer@example.com",
        fields={"property_address": "1 Main St", "assignment_fee": Decimal("15000")},
        idempotency_key="assignment:1",
    )
    assert [r.role_name for r in req.recipients] == ["Assignee", "Assignor"]
    assert req.prefill_tabs["assignment_fee"] == "15,000.00"
    assert req.template_key == "assignment"


# -- Connect ---------------------------------------------------------------------------------------

CONNECT_PAYLOAD = {
    "event": "envelope-completed",
    "apiVersion": "v2.1",
    "uri": "/restapi/v2.1/accounts/acc-1/envelopes/env-1",
    "retryCount": 0,
    "configurationId": 123,
    "generatedDateTime": "2026-10-27T15:00:00.000Z",
    "data": {
        "accountId": "acc-1",
        "userId": "user-1",
        "envelopeId": "env-1",
        "envelopeSummary": {"status": "completed", "emailSubject": "PSA"},
    },
}


def test_connect_event_parses_wire_names_and_maps_status() -> None:
    event = ConnectEvent.model_validate(CONNECT_PAYLOAD)
    assert event.envelope_id == "env-1"
    assert event.is_envelope_event
    assert event.envelope_status is EnvelopeStatus.COMPLETED
    assert event.data.envelope_summary is not None
    assert event.data.envelope_summary.email_subject == "PSA"


def test_connect_event_recipient_event_falls_back_to_summary_status() -> None:
    payload = {**CONNECT_PAYLOAD, "event": "recipient-completed"}
    payload["data"] = {**CONNECT_PAYLOAD["data"], "envelopeSummary": {"status": "delivered"}}
    event = ConnectEvent.model_validate(payload)
    assert not event.is_envelope_event
    assert event.envelope_status is EnvelopeStatus.DELIVERED


def test_connect_event_requires_envelope_id() -> None:
    payload = {**CONNECT_PAYLOAD, "data": {"accountId": "acc-1"}}
    with pytest.raises(ValidationError):
        ConnectEvent.model_validate(payload)


def test_connect_hmac_is_base64_sha256_and_constant_time_verified() -> None:
    body = json.dumps(CONNECT_PAYLOAD).encode()
    sig = connect_signature(body, "secret-key")
    assert verify_connect_signature(body, "secret-key", sig)
    assert verify_connect_signature(body, "secret-key", f" {sig} ")  # header whitespace tolerated
    assert not verify_connect_signature(body, "other-key", sig)
    assert not verify_connect_signature(body + b" ", "secret-key", sig)  # raw body matters
    assert not verify_connect_signature(body, "secret-key", None)
