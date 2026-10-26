"""DocuSign eSignature data shapes (REQUIREMENTS.md INT-16, SEL-10; docs/integrations/docusign.md).

Only what the disposition path needs: an envelope created *from a template* with named
recipient roles and pre-filled text tabs, its status, and the Connect (webhook) event payload.
Nothing here talks to the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnvelopeStatus(StrEnum):
    """DocuSign envelope ``status`` values this integration handles.

    ``created`` is a draft envelope that has not been sent; ``sent`` / ``delivered`` are in
    process (and may be voided); ``completed`` means every recipient signed; ``declined`` and
    ``voided`` are terminal failures.
    """

    CREATED = "created"
    SENT = "sent"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    DECLINED = "declined"
    VOIDED = "voided"

    @property
    def is_terminal(self) -> bool:
        return self in (EnvelopeStatus.COMPLETED, EnvelopeStatus.DECLINED, EnvelopeStatus.VOIDED)

    @property
    def is_in_process(self) -> bool:
        return self in (EnvelopeStatus.SENT, EnvelopeStatus.DELIVERED)


# Allowed transitions for the mock's state machine (DocuSign's own lifecycle, simplified).
ENVELOPE_TRANSITIONS: dict[EnvelopeStatus, frozenset[EnvelopeStatus]] = {
    EnvelopeStatus.CREATED: frozenset({EnvelopeStatus.SENT, EnvelopeStatus.VOIDED}),
    EnvelopeStatus.SENT: frozenset(
        {EnvelopeStatus.DELIVERED, EnvelopeStatus.COMPLETED, EnvelopeStatus.DECLINED}
        | {EnvelopeStatus.VOIDED}
    ),
    EnvelopeStatus.DELIVERED: frozenset(
        {EnvelopeStatus.COMPLETED, EnvelopeStatus.DECLINED, EnvelopeStatus.VOIDED}
    ),
    EnvelopeStatus.COMPLETED: frozenset(),
    EnvelopeStatus.DECLINED: frozenset(),
    EnvelopeStatus.VOIDED: frozenset(),
}


class TemplateRecipient(BaseModel):
    """One ``templateRoles[]`` entry: the person who fills a role defined on the template.

    ``tabs`` are the recipient's text tabs keyed by ``tabLabel`` (the label set on the template
    by the attorney, PRE-13); the gateway renders them as
    ``tabs.textTabs[{tabLabel, value}]`` on that role.
    """

    model_config = ConfigDict(frozen=True)

    role_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    tabs: dict[str, str] = Field(default_factory=dict)
    routing_order: int = 1


class EnvelopeRequest(BaseModel):
    """What the application asks DocuSign to create. Always from a template (INT-16)."""

    model_config = ConfigDict(frozen=True)

    template_key: str  # registry key: "psa" | "assignment" (templates.py)
    template_id: str  # the DocuSign templateId configured for that key
    email_subject: str
    recipients: list[TemplateRecipient] = Field(min_length=1)
    # Envelope-level ``prefillTabs`` (sender-filled, not tied to a recipient).
    prefill_tabs: dict[str, str] = Field(default_factory=dict)
    # "sent" delivers immediately; "created" leaves a draft envelope in the DocuSign account.
    # The default here is "created" on purpose: a human sends from DocuSign (POL-03i).
    status: EnvelopeStatus = EnvelopeStatus.CREATED
    email_blurb: str | None = None
    # Our own idempotency key; the mock dedupes on it and the live client records it in
    # ``customFields`` so a retried call can be matched to the envelope it already created.
    idempotency_key: str | None = None

    def to_api_payload(self) -> dict[str, Any]:
        """The eSignature REST v2.1 ``envelopeDefinition`` body."""
        roles: list[dict[str, Any]] = []
        for r in self.recipients:
            role: dict[str, Any] = {
                "roleName": r.role_name,
                "name": r.name,
                "email": r.email,
                "routingOrder": str(r.routing_order),
            }
            if r.tabs:
                role["tabs"] = {
                    "textTabs": [{"tabLabel": k, "value": v} for k, v in sorted(r.tabs.items())]
                }
            roles.append(role)
        payload: dict[str, Any] = {
            "templateId": self.template_id,
            "emailSubject": self.email_subject,
            "templateRoles": roles,
            "status": self.status.value,
        }
        if self.email_blurb:
            payload["emailBlurb"] = self.email_blurb
        if self.prefill_tabs:
            payload["prefillTabs"] = {
                "textTabs": [
                    {"tabLabel": k, "value": v} for k, v in sorted(self.prefill_tabs.items())
                ]
            }
        if self.idempotency_key:
            payload["customFields"] = {
                "textCustomFields": [
                    {"name": "idempotency_key", "value": self.idempotency_key, "show": "false"}
                ]
            }
        return payload


class EnvelopeInfo(BaseModel):
    """What we keep about an envelope (``envelopeId`` / ``status`` / ``statusDateTime``)."""

    model_config = ConfigDict(frozen=True)

    envelope_id: str
    status: EnvelopeStatus
    status_at: datetime | None = None
    template_key: str | None = None
    uri: str | None = None
    voided_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TemplateInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    template_id: str
    name: str
    shared: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------
# Connect (webhook) event: JSON "SIM" delivery, eventData version restv2.1
# --------------------------------------------------------------------------------------------

CONNECT_SIGNATURE_HEADER = "X-DocuSign-Signature-1"

# Envelope-level Connect event names (the recipient-* events are not consumed here).
CONNECT_ENVELOPE_EVENTS: frozenset[str] = frozenset(
    {
        "envelope-sent",
        "envelope-resent",
        "envelope-delivered",
        "envelope-completed",
        "envelope-declined",
        "envelope-voided",
        "envelope-corrected",
    }
)

_EVENT_TO_STATUS: dict[str, EnvelopeStatus] = {
    "envelope-sent": EnvelopeStatus.SENT,
    "envelope-resent": EnvelopeStatus.SENT,
    "envelope-delivered": EnvelopeStatus.DELIVERED,
    "envelope-completed": EnvelopeStatus.COMPLETED,
    "envelope-declined": EnvelopeStatus.DECLINED,
    "envelope-voided": EnvelopeStatus.VOIDED,
}


class ConnectEnvelopeSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")

    status: str
    email_subject: str | None = Field(default=None, alias="emailSubject")
    status_changed_date_time: str | None = Field(default=None, alias="statusChangedDateTime")


class ConnectEventData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)

    account_id: str = Field(alias="accountId")
    envelope_id: str = Field(alias="envelopeId")
    user_id: str | None = Field(default=None, alias="userId")
    envelope_summary: ConnectEnvelopeSummary | None = Field(default=None, alias="envelopeSummary")


class ConnectEvent(BaseModel):
    """One Connect JSON (SIM) notification. Field names follow the wire format."""

    model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)

    event: str
    api_version: str | None = Field(default=None, alias="apiVersion")
    uri: str | None = None
    retry_count: int = Field(default=0, alias="retryCount")
    configuration_id: int | str | None = Field(default=None, alias="configurationId")
    generated_date_time: str | None = Field(default=None, alias="generatedDateTime")
    data: ConnectEventData

    @property
    def envelope_id(self) -> str:
        return self.data.envelope_id

    @property
    def is_envelope_event(self) -> bool:
        return self.event in CONNECT_ENVELOPE_EVENTS

    @property
    def envelope_status(self) -> EnvelopeStatus | None:
        """The status implied by the event (falls back to ``envelopeSummary.status``)."""
        mapped = _EVENT_TO_STATUS.get(self.event)
        if mapped is not None:
            return mapped
        summary = self.data.envelope_summary
        if summary is None:
            return None
        try:
            return EnvelopeStatus(summary.status.lower())
        except ValueError:
            return None


def connect_signature(raw_body: bytes, key: str) -> str:
    """``Base64(HMAC-SHA256(raw_body, key))`` - what DocuSign puts in X-DocuSign-Signature-N."""
    digest = hmac.new(key.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_connect_signature(raw_body: bytes, key: str, header_value: str | None) -> bool:
    """Constant-time check of one signature header against one Connect HMAC key."""
    if not header_value:
        return False
    expected = connect_signature(raw_body, key)
    return hmac.compare_digest(expected.encode("ascii"), header_value.strip().encode("ascii"))


__all__ = [
    "CONNECT_ENVELOPE_EVENTS",
    "CONNECT_SIGNATURE_HEADER",
    "ENVELOPE_TRANSITIONS",
    "ConnectEnvelopeSummary",
    "ConnectEvent",
    "ConnectEventData",
    "EnvelopeInfo",
    "EnvelopeRequest",
    "EnvelopeStatus",
    "TemplateInfo",
    "TemplateRecipient",
    "connect_signature",
    "verify_connect_signature",
]
