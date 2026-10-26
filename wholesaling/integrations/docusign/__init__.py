"""DocuSign eSignature integration: Protocol, models, templates registry, mock, live client.

Selected by ``DOCUSIGN_MODE`` (``mock`` by default). See docs/integrations/docusign.md.

Only the operator's ``/deals`` page calls this gateway (REQUIREMENTS.md POL-03i, XC-05,
RUL-06): contract execution is a hard floor, so no workflow or agent imports it.
"""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.docusign.interface import (
    DocuSignAuthError,
    DocuSignCircuitOpen,
    DocuSignError,
    DocuSignGateway,
    DocuSignInvalidTransition,
    DocuSignNotFound,
    DocuSignTemplateUnconfigured,
    DocuSignThrottled,
    RecordedCall,
)
from wholesaling.integrations.docusign.mock import MockDocuSignGateway
from wholesaling.integrations.docusign.models import (
    CONNECT_SIGNATURE_HEADER,
    ConnectEvent,
    EnvelopeInfo,
    EnvelopeRequest,
    EnvelopeStatus,
    TemplateInfo,
    TemplateRecipient,
    connect_signature,
    verify_connect_signature,
)
from wholesaling.integrations.docusign.templates import (
    ASSIGNMENT_TEMPLATE,
    PSA_TEMPLATE,
    TEMPLATES,
    TemplateSpec,
    build_assignment_request,
    build_psa_request,
    get_template,
)


def get_docusign(settings: Settings) -> DocuSignGateway:
    """Return the gateway implementation selected by ``DOCUSIGN_MODE``."""
    if settings.docusign_mode == "mock":
        key = (
            settings.docusign_connect_hmac_key.get_secret_value()
            if settings.docusign_connect_hmac_key is not None
            else "mock-connect-key"
        )
        return MockDocuSignGateway(
            account_id=settings.docusign_account_id or "mock-account", connect_hmac_key=key
        )
    # Imported lazily so mock-mode processes never import the live module (RUL-02 pattern).
    from wholesaling.integrations.docusign.live import LiveDocuSignGateway

    return LiveDocuSignGateway(settings)


def template_id_for(settings: Settings, template_key: str) -> str:
    """The configured DocuSign templateId for a registry key; the mock accepts a placeholder."""
    spec = get_template(template_key)
    configured = getattr(settings, spec.settings_field, None)
    if configured:
        return str(configured)
    if settings.docusign_mode == "mock":
        return f"mock-template-{spec.key}"
    msg = f"{spec.settings_field.upper()} is not set (PRE-13); cannot create a {spec.key} envelope"
    raise DocuSignTemplateUnconfigured(msg)


__all__ = [
    "ASSIGNMENT_TEMPLATE",
    "CONNECT_SIGNATURE_HEADER",
    "PSA_TEMPLATE",
    "TEMPLATES",
    "ConnectEvent",
    "DocuSignAuthError",
    "DocuSignCircuitOpen",
    "DocuSignError",
    "DocuSignGateway",
    "DocuSignInvalidTransition",
    "DocuSignNotFound",
    "DocuSignTemplateUnconfigured",
    "DocuSignThrottled",
    "EnvelopeInfo",
    "EnvelopeRequest",
    "EnvelopeStatus",
    "MockDocuSignGateway",
    "RecordedCall",
    "TemplateInfo",
    "TemplateRecipient",
    "TemplateSpec",
    "build_assignment_request",
    "build_psa_request",
    "connect_signature",
    "get_docusign",
    "get_template",
    "template_id_for",
    "verify_connect_signature",
]
