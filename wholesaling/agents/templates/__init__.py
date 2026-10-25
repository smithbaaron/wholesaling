"""Locked outreach templates (REQUIREMENTS.md CPY-01..05, XC-07, CMP-01).

Templates are module constants, never model output. ``render`` is the only way to turn one into
text: it refuses unknown placeholders and any parameter or rendered text that would assert
inventory, contracts or exclusivity (XC-07).
"""

from wholesaling.agents.templates.investor_outreach import (
    APPROVED,
    FOLLOWUP_BODIES,
    INITIAL_BODY,
    INITIAL_SUBJECTS,
    INITIAL_TEMPLATE,
    PENDING_APPROVAL,
    PROHIBITED_PHRASES,
    ProhibitedClaimError,
    SenderIdentity,
    Template,
    TemplateRenderError,
    footer,
    placeholders,
    render,
)

__all__ = [
    "APPROVED",
    "FOLLOWUP_BODIES",
    "INITIAL_BODY",
    "INITIAL_SUBJECTS",
    "INITIAL_TEMPLATE",
    "PENDING_APPROVAL",
    "PROHIBITED_PHRASES",
    "ProhibitedClaimError",
    "SenderIdentity",
    "Template",
    "TemplateRenderError",
    "footer",
    "placeholders",
    "render",
]
