"""In-memory DocuSign gateway with an envelope state machine (``DOCUSIGN_MODE=mock``).

Every call is recorded. Envelopes move through ``created -> sent -> delivered -> completed``
(or ``declined`` / ``voided``) only when a test or the operator advances them
(``advance`` / ``complete`` / ``decline``): nothing in the mock signs anything on its own, so a
contract row can never reach ``signed`` without the human route (``/deals``).

``connect_event_for`` builds the Connect JSON (SIM) payload DocuSign would POST for the
envelope's current status, so the webhook model can be exercised end to end without a network.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.docusign.interface import (
    DocuSignInvalidTransition,
    DocuSignNotFound,
    RecordedCall,
)
from wholesaling.integrations.docusign.models import (
    ENVELOPE_TRANSITIONS,
    ConnectEvent,
    EnvelopeInfo,
    EnvelopeRequest,
    EnvelopeStatus,
    TemplateInfo,
    connect_signature,
)
from wholesaling.integrations.docusign.templates import TEMPLATES

Clock = Callable[[], datetime]

_STATUS_TO_EVENT: dict[EnvelopeStatus, str] = {
    EnvelopeStatus.SENT: "envelope-sent",
    EnvelopeStatus.DELIVERED: "envelope-delivered",
    EnvelopeStatus.COMPLETED: "envelope-completed",
    EnvelopeStatus.DECLINED: "envelope-declined",
    EnvelopeStatus.VOIDED: "envelope-voided",
}


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class MockDocuSignGateway:
    name = "docusign"
    mode: IntegrationMode = "mock"

    def __init__(
        self,
        *,
        account_id: str = "mock-account",
        clock: Clock | None = None,
        connect_hmac_key: str = "mock-connect-key",
    ) -> None:
        self.account_id = account_id
        self._clock: Clock = clock or _utcnow
        self.connect_hmac_key = connect_hmac_key
        self.calls: list[RecordedCall] = []
        self.envelopes: dict[str, EnvelopeInfo] = {}
        self.requests: dict[str, EnvelopeRequest] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self.templates: list[TemplateInfo] = [
            TemplateInfo(template_id=f"mock-template-{spec.key}", name=spec.display_name)
            for spec in TEMPLATES.values()
        ]
        self.healthy = True

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append(RecordedCall(method=method, args=args))

    def reset(self) -> None:
        self.calls.clear()
        self.envelopes.clear()
        self.requests.clear()
        self._by_idempotency_key.clear()

    # -- gateway surface ---------------------------------------------------------------------

    async def healthcheck(self) -> HealthStatus:
        self._record("healthcheck")
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=self.healthy,
            detail="mock gateway" if self.healthy else "mock gateway forced unhealthy",
        )

    async def create_envelope_from_template(self, request: EnvelopeRequest) -> EnvelopeInfo:
        self._record(
            "create_envelope_from_template",
            template_key=request.template_key,
            template_id=request.template_id,
            status=request.status.value,
            recipients=[r.email for r in request.recipients],
            idempotency_key=request.idempotency_key,
        )
        if request.idempotency_key and request.idempotency_key in self._by_idempotency_key:
            return self.envelopes[self._by_idempotency_key[request.idempotency_key]]
        if request.status not in (EnvelopeStatus.CREATED, EnvelopeStatus.SENT):
            msg = f"an envelope can only be created as 'created' or 'sent', not {request.status}"
            raise DocuSignInvalidTransition(msg)
        envelope_id = str(uuid.uuid4())
        info = EnvelopeInfo(
            envelope_id=envelope_id,
            status=request.status,
            status_at=self._clock(),
            template_key=request.template_key,
            uri=f"/envelopes/{envelope_id}",
            raw={"payload": request.to_api_payload()},
        )
        self.envelopes[envelope_id] = info
        self.requests[envelope_id] = request
        if request.idempotency_key:
            self._by_idempotency_key[request.idempotency_key] = envelope_id
        return info

    async def get_envelope_status(self, envelope_id: str) -> EnvelopeInfo:
        self._record("get_envelope_status", envelope_id=envelope_id)
        return self._require(envelope_id)

    async def void_envelope(self, envelope_id: str, reason: str) -> EnvelopeInfo:
        self._record("void_envelope", envelope_id=envelope_id, reason=reason)
        if not reason.strip():
            msg = "voidedReason is required"
            raise DocuSignInvalidTransition(msg)
        info = self._require(envelope_id)
        if not info.status.is_in_process and info.status is not EnvelopeStatus.CREATED:
            msg = f"envelope {envelope_id} is {info.status.value}; only in-process envelopes void"
            raise DocuSignInvalidTransition(msg)
        return self._set(envelope_id, EnvelopeStatus.VOIDED, voided_reason=reason)

    async def list_templates(self) -> list[TemplateInfo]:
        self._record("list_templates")
        return list(self.templates)

    # -- state machine controls (tests / operator simulation; never called by the app) -------

    def advance(self, envelope_id: str, status: EnvelopeStatus) -> EnvelopeInfo:
        """Move the envelope to ``status`` if DocuSign's lifecycle allows it."""
        info = self._require(envelope_id)
        if status not in ENVELOPE_TRANSITIONS[info.status]:
            msg = f"envelope {envelope_id}: {info.status.value} -> {status.value} is not allowed"
            raise DocuSignInvalidTransition(msg)
        return self._set(envelope_id, status)

    def send(self, envelope_id: str) -> EnvelopeInfo:
        return self.advance(envelope_id, EnvelopeStatus.SENT)

    def deliver(self, envelope_id: str) -> EnvelopeInfo:
        info = self._require(envelope_id)
        if info.status is EnvelopeStatus.CREATED:
            self.advance(envelope_id, EnvelopeStatus.SENT)
        return self.advance(envelope_id, EnvelopeStatus.DELIVERED)

    def complete(self, envelope_id: str) -> EnvelopeInfo:
        """Every recipient signed. ``created`` envelopes pass through ``sent`` first."""
        info = self._require(envelope_id)
        if info.status is EnvelopeStatus.CREATED:
            self.advance(envelope_id, EnvelopeStatus.SENT)
        return self.advance(envelope_id, EnvelopeStatus.COMPLETED)

    def decline(self, envelope_id: str) -> EnvelopeInfo:
        info = self._require(envelope_id)
        if info.status is EnvelopeStatus.CREATED:
            self.advance(envelope_id, EnvelopeStatus.SENT)
        return self.advance(envelope_id, EnvelopeStatus.DECLINED)

    def connect_event_for(self, envelope_id: str, *, retry_count: int = 0) -> ConnectEvent:
        """The Connect JSON (SIM) notification for the envelope's current status."""
        info = self._require(envelope_id)
        event = _STATUS_TO_EVENT.get(info.status, "envelope-corrected")
        request = self.requests.get(envelope_id)
        payload: dict[str, Any] = {
            "event": event,
            "apiVersion": "v2.1",
            "uri": f"/restapi/v2.1/accounts/{self.account_id}/envelopes/{envelope_id}",
            "retryCount": retry_count,
            "configurationId": 1,
            "generatedDateTime": self._clock().isoformat(),
            "data": {
                "accountId": self.account_id,
                "userId": "mock-user",
                "envelopeId": envelope_id,
                "envelopeSummary": {
                    "status": info.status.value,
                    "emailSubject": request.email_subject if request else None,
                    "statusChangedDateTime": (
                        info.status_at.isoformat() if info.status_at else None
                    ),
                },
            },
        }
        return ConnectEvent.model_validate(payload)

    def connect_delivery(self, envelope_id: str) -> tuple[bytes, dict[str, str]]:
        """``(raw_body, headers)`` as the webhook route would receive them, HMAC-signed."""
        event = self.connect_event_for(envelope_id)
        body = event.model_dump_json(by_alias=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-DocuSign-Signature-1": connect_signature(body, self.connect_hmac_key),
        }
        return body, headers

    # -- internals -------------------------------------------------------------------------

    def _require(self, envelope_id: str) -> EnvelopeInfo:
        info = self.envelopes.get(envelope_id)
        if info is None:
            msg = f"envelope {envelope_id} not found"
            raise DocuSignNotFound(msg, status_code=404)
        return info

    def _set(
        self, envelope_id: str, status: EnvelopeStatus, *, voided_reason: str | None = None
    ) -> EnvelopeInfo:
        current = self._require(envelope_id)
        updated = current.model_copy(
            update={
                "status": status,
                "status_at": self._clock(),
                "voided_reason": voided_reason or current.voided_reason,
            }
        )
        self.envelopes[envelope_id] = updated
        return updated


__all__ = ["MockDocuSignGateway"]
