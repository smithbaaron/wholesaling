"""What the application needs from DocuSign eSignature, kept deliberately narrow (INT-16, RUL-02).

Everything outside ``integrations.docusign`` talks to ``DocuSignGateway`` so it runs unchanged
against the mock (``DOCUSIGN_MODE=mock``, the default) and the live client.

Hard rule (REQUIREMENTS.md XC-05, POL-03i, RUL-06): nothing on this surface is ever called from
an automated path. Envelope creation is triggered by a human on ``/deals``; the policy engine
returns ``queue_for_human`` for ``CONTRACT_EXECUTION`` at every level, and no workflow or agent
imports this module. The gateway is a tool the operator uses through the UI, not an actor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.docusign.models import EnvelopeInfo, EnvelopeRequest, TemplateInfo


class DocuSignError(Exception):
    """Base class for every error the gateway raises."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class DocuSignAuthError(DocuSignError):
    """JWT grant failed, consent is missing, or the API rejected the token (401/403)."""


class DocuSignNotFound(DocuSignError):
    """Envelope or template does not exist (404)."""


class DocuSignThrottled(DocuSignError):
    """429 after the retry budget was spent."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class DocuSignCircuitOpen(DocuSignError):
    """The circuit breaker is open; no request was attempted (XC-08)."""


class DocuSignInvalidTransition(DocuSignError):
    """The envelope is not in a state that allows the operation (e.g. voiding a completed one)."""


class DocuSignTemplateUnconfigured(DocuSignError):
    """The registry knows the template but no templateId is configured for it (PRE-13)."""


@dataclass(slots=True)
class RecordedCall:
    method: str
    args: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class DocuSignGateway(Protocol):
    name: str
    mode: IntegrationMode

    async def healthcheck(self) -> HealthStatus: ...

    async def create_envelope_from_template(self, request: EnvelopeRequest) -> EnvelopeInfo:
        """``POST /accounts/{accountId}/envelopes`` with ``templateId`` + ``templateRoles``.

        Human-triggered only. With ``request.status == created`` the envelope is a draft in the
        DocuSign account; with ``sent`` DocuSign emails the first recipient immediately.
        """
        ...

    async def get_envelope_status(self, envelope_id: str) -> EnvelopeInfo:
        """``GET /accounts/{accountId}/envelopes/{envelopeId}``. Raises ``DocuSignNotFound``."""
        ...

    async def void_envelope(self, envelope_id: str, reason: str) -> EnvelopeInfo:
        """``PUT /accounts/{accountId}/envelopes/{envelopeId}`` with ``status: voided`` and the
        required ``voidedReason``. Only in-process envelopes can be voided."""
        ...

    async def list_templates(self) -> list[TemplateInfo]:
        """``GET /accounts/{accountId}/templates``."""
        ...


__all__ = [
    "DocuSignAuthError",
    "DocuSignCircuitOpen",
    "DocuSignError",
    "DocuSignGateway",
    "DocuSignInvalidTransition",
    "DocuSignNotFound",
    "DocuSignTemplateUnconfigured",
    "DocuSignThrottled",
    "RecordedCall",
]
