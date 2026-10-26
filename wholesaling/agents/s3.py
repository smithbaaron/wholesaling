"""S3 Seller Outreach (REQUIREMENTS.md AGT-S3, SEL-04, SEL-05, POL-03d/e, STK-13, RUL-06).

Mirrors A3 for the seller side: step 0 is the initial, steps 1..3 the follow-ups at 3 / 7 / 14
days (``config/sequences.yaml`` ``seller_followup_days``), all in one Outlook conversation. No
model is involved: the copy is fixed (pending PRE-16), S3 fills ``{owner_first_name}``,
``{property_address}``, ``{market}``, ``{sender_name}``, ``{company}`` and appends the SEL-05
compliance block (TX equitable-interest disclosure + identity + postal address + opt-out) to
every message through ``render_seller_message``.

Every step's policy action is ``seller_outreach_email`` (POL-03d). SMS: ``seller_outreach_sms``
is OFF at L0 and never sends in this build (POL-03e, RUL-06). ``SmsGateway`` below is the
interface a later 10DLC build would implement; the only implementation, ``DisabledSmsGateway``,
raises on any send so no code path can accidentally text a seller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

from wholesaling.agents.templates.investor_outreach import (
    ApprovalStatus,
    SenderIdentity,
    footer,
    render,
)
from wholesaling.agents.templates.seller_outreach import (
    FALLBACK_FIRST_NAME,
    FOLLOWUP_TEMPLATES,
    INITIAL_TEMPLATE,
    SELLER_SUBJECTS,
    LegalReview,
    SellerTemplate,
    render_seller_message,
)
from wholesaling.config.tunables import Tunables, get_tunables
from wholesaling.db.models.enums import AgentId
from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.policy.actions import PolicyAction

ThreadStrategy = Literal["new_conversation", "reply_in_conversation"]
TOTAL_STEPS: Final[int] = 1 + len(FOLLOWUP_TEMPLATES)


@dataclass(frozen=True, slots=True)
class SellerOutreachDraft:
    step: int
    subject: str
    body: str  # rendered copy + disclosure + CAN-SPAM footer
    to: str
    thread_strategy: ThreadStrategy
    template_key: str
    approval_status: ApprovalStatus
    legal_review: LegalReview
    source_row: str
    subject_variant: int | None
    policy_action: PolicyAction


@dataclass(frozen=True, slots=True)
class SellerSequencePlan:
    """Send schedule: OPN-05 default 3 / 7 / 14 days."""

    intervals_days: tuple[int, ...]

    @property
    def total_steps(self) -> int:
        return len(self.intervals_days) + 1


class S3SellerOutreach:
    agent_id: Final = AgentId.S3

    def __init__(self, sender: SenderIdentity, tunables: Tunables | None = None) -> None:
        self._sender = sender
        self._tunables = tunables or get_tunables()
        footer(sender)  # fail early (CMP-01): no drafts without address + opt-out

    def sequence_plan(self) -> SellerSequencePlan:
        intervals = tuple(int(d) for d in self._tunables.sequences.seller_followup_days)
        if len(intervals) != len(FOLLOWUP_TEMPLATES):
            msg = (
                f"config/sequences.yaml seller_followup_days has {len(intervals)} intervals "
                f"but there are {len(FOLLOWUP_TEMPLATES)} follow-up templates"
            )
            raise ValueError(msg)
        if any(d <= 0 for d in intervals):
            msg = "seller_followup_days must be positive"
            raise ValueError(msg)
        return SellerSequencePlan(intervals_days=intervals)

    @staticmethod
    def subject_variant(seller_lead_id: uuid.UUID) -> int:
        return seller_lead_id.int % len(SELLER_SUBJECTS)

    def template_for_step(self, step: int) -> SellerTemplate:
        if step == 0:
            return INITIAL_TEMPLATE
        if 1 <= step <= len(FOLLOWUP_TEMPLATES):
            return FOLLOWUP_TEMPLATES[step - 1]
        msg = f"step must be 0..{len(FOLLOWUP_TEMPLATES)}, got {step}"
        raise ValueError(msg)

    def draft(
        self,
        *,
        step: int,
        seller_lead_id: uuid.UUID,
        contact_email: str,
        property_address: str,
        market: str,
        owner_first_name: str | None,
    ) -> SellerOutreachDraft:
        template = self.template_for_step(step)
        name = (owner_first_name or "").strip() or FALLBACK_FIRST_NAME
        body = render_seller_message(
            template,
            self._sender,
            owner_first_name=name,
            property_address=property_address.strip(),
            market=market.strip(),
        )
        variant = self.subject_variant(seller_lead_id)
        subject = render(SELLER_SUBJECTS[variant], property_address=property_address.strip())
        return SellerOutreachDraft(
            step=step,
            subject=subject if step == 0 else f"RE: {subject}",
            body=body,
            to=contact_email,
            thread_strategy="new_conversation" if step == 0 else "reply_in_conversation",
            template_key=template.key,
            approval_status=template.approval_status,
            legal_review=template.legal_review,
            source_row=template.source_row,
            subject_variant=variant if step == 0 else None,
            policy_action=PolicyAction.SELLER_OUTREACH_EMAIL,
        )

    def sequence(
        self,
        *,
        seller_lead_id: uuid.UUID,
        contact_email: str,
        property_address: str,
        market: str,
        owner_first_name: str | None,
    ) -> list[SellerOutreachDraft]:
        return [
            self.draft(
                step=step,
                seller_lead_id=seller_lead_id,
                contact_email=contact_email,
                property_address=property_address,
                market=market,
                owner_first_name=owner_first_name,
            )
            for step in range(TOTAL_STEPS)
        ]


# --------------------------------------------------------------------------------------------
# SMS: interface only (POL-03e OFF, RUL-06)
# --------------------------------------------------------------------------------------------


class SmsDisabledError(RuntimeError):
    """Raised by every send attempt: SMS is off in this build."""


@runtime_checkable
class SmsGateway(Protocol):
    name: str
    mode: IntegrationMode

    async def send(self, to: str, body: str, *, consent_ref: str) -> str:
        """Send one text; returns the provider message id. Requires a consent record (POL-03e)."""
        ...

    async def healthcheck(self) -> HealthStatus: ...


class DisabledSmsGateway:
    """The only SMS gateway that exists. ``send`` always raises (RUL-06)."""

    name = "sms"
    mode: IntegrationMode = "off"

    def __init__(self) -> None:
        self.attempts: list[dict[str, str]] = []

    async def send(self, to: str, body: str, *, consent_ref: str) -> str:
        self.attempts.append({"to": to, "consent_ref": consent_ref})
        msg = (
            "SMS is disabled in this build (REQUIREMENTS.md RUL-06, POL-03e): no 10DLC "
            "registration, no consent basis, no sends"
        )
        raise SmsDisabledError(msg)

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(name=self.name, mode=self.mode, ok=True, detail="disabled by policy")


__all__ = [
    "TOTAL_STEPS",
    "DisabledSmsGateway",
    "S3SellerOutreach",
    "SellerOutreachDraft",
    "SellerSequencePlan",
    "SmsDisabledError",
    "SmsGateway",
    "ThreadStrategy",
]
