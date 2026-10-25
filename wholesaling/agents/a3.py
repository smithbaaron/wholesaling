"""A3 Investor Outreach (REQUIREMENTS.md INV-10, INV-11, CPY-01..04, XC-07).

A3 produces ``OutreachDraft`` objects from the locked templates in
``wholesaling.agents.templates``: step 0 is the approved CPY-01 initial, steps 1..3 are the
follow-up drafts (pending PRE-16 approval). It never calls a model: the copy is fixed, the only
variables are the investor's first name, the market, the sender identity and the subject
variant, which is assigned deterministically per investor (hash of the investor id) so an INV-11
experiment is reproducible and the operator is never shown a list of subject options.

Thread strategy: step 0 starts a new Outlook conversation; steps 1..3 are replies inside that
conversation (INV-23 "one Outlook conversation").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

from wholesaling.agents.templates.investor_outreach import (
    FOLLOWUP_BODIES,
    INITIAL_SUBJECTS,
    INITIAL_TEMPLATE,
    ApprovalStatus,
    SenderIdentity,
    Template,
    footer,
    render,
)
from wholesaling.config.tunables import Tunables, get_tunables
from wholesaling.db.models.enums import AgentId
from wholesaling.policy.actions import PolicyAction

ThreadStrategy = Literal["new_conversation", "reply_in_conversation"]

# Greeting fallback when A2 found no first name. CPY-01 is first-name parameterised; "there"
# keeps the approved sentence structure without inventing a name.
FALLBACK_FIRST_NAME: Final[str] = "there"
TOTAL_STEPS: Final[int] = 1 + len(FOLLOWUP_BODIES)


@dataclass(frozen=True, slots=True)
class OutreachDraft:
    step: int
    subject: str
    body: str  # rendered template + CAN-SPAM footer
    to: str
    thread_strategy: ThreadStrategy
    template_key: str
    approval_status: ApprovalStatus
    source_row: str
    subject_variant: int | None  # index into INITIAL_SUBJECTS; None for follow-ups
    policy_action: PolicyAction


@dataclass(frozen=True, slots=True)
class SequencePlan:
    """Send schedule from ``config/sequences.yaml`` (OPN-04 default 4/4/4 days)."""

    intervals_days: tuple[int, ...]

    @property
    def total_steps(self) -> int:
        return len(self.intervals_days) + 1


class A3Outreach:
    agent_id: Final = AgentId.A3

    def __init__(self, sender: SenderIdentity, tunables: Tunables | None = None) -> None:
        self._sender = sender
        self._tunables = tunables or get_tunables()
        # Fail early (CMP-01): no drafts without a postal address and an opt-out line.
        self._footer = footer(sender)

    # -- planning ------------------------------------------------------------------------------

    def sequence_plan(self) -> SequencePlan:
        intervals = tuple(int(d) for d in self._tunables.sequences.investor_followup_days)
        if len(intervals) != len(FOLLOWUP_BODIES):
            msg = (
                f"config/sequences.yaml investor_followup_days has {len(intervals)} intervals "
                f"but there are {len(FOLLOWUP_BODIES)} follow-up templates"
            )
            raise ValueError(msg)
        if any(d <= 0 for d in intervals):
            msg = "investor_followup_days must be positive"
            raise ValueError(msg)
        return SequencePlan(intervals_days=intervals)

    # -- subject variants (INV-11) -------------------------------------------------------------

    @staticmethod
    def subject_variant(investor_id: uuid.UUID) -> int:
        return investor_id.int % len(INITIAL_SUBJECTS)

    # -- drafting ------------------------------------------------------------------------------

    def template_for_step(self, step: int) -> Template:
        if step == 0:
            return INITIAL_TEMPLATE
        if 1 <= step <= len(FOLLOWUP_BODIES):
            return FOLLOWUP_BODIES[step - 1]
        msg = f"step must be 0..{len(FOLLOWUP_BODIES)}, got {step}"
        raise ValueError(msg)

    def draft(
        self,
        *,
        step: int,
        investor_id: uuid.UUID,
        contact_email: str,
        market: str,
        first_name: str | None,
    ) -> OutreachDraft:
        template = self.template_for_step(step)
        name = (first_name or "").strip() or FALLBACK_FIRST_NAME
        available = {
            "first_name": name,
            "market": market.strip(),
            "sender_name": self._sender.name,
        }
        # A template may use a subset (the last follow-up names no market).
        params = {k: v for k, v in available.items() if k in template.placeholders}
        body = render(template, **params) + "\n\n" + self._footer
        variant = self.subject_variant(investor_id)
        initial_subject = render(INITIAL_SUBJECTS[variant], market=market.strip())
        if step == 0:
            return OutreachDraft(
                step=0,
                subject=initial_subject,
                body=body,
                to=contact_email,
                thread_strategy="new_conversation",
                template_key=template.key,
                approval_status=template.approval_status,
                source_row=template.source_row,
                subject_variant=variant,
                policy_action=PolicyAction.INVESTOR_INITIAL_OUTREACH,
            )
        return OutreachDraft(
            step=step,
            # Graph sets the reply subject itself; this is what the record shows.
            subject=f"RE: {initial_subject}",
            body=body,
            to=contact_email,
            thread_strategy="reply_in_conversation",
            template_key=template.key,
            approval_status=template.approval_status,
            source_row=template.source_row,
            subject_variant=None,
            policy_action=PolicyAction.INVESTOR_FOLLOWUP,
        )

    def sequence(
        self,
        *,
        investor_id: uuid.UUID,
        contact_email: str,
        market: str,
        first_name: str | None,
    ) -> list[OutreachDraft]:
        """All four drafts, in send order."""
        return [
            self.draft(
                step=step,
                investor_id=investor_id,
                contact_email=contact_email,
                market=market,
                first_name=first_name,
            )
            for step in range(TOTAL_STEPS)
        ]


__all__ = [
    "FALLBACK_FIRST_NAME",
    "TOTAL_STEPS",
    "A3Outreach",
    "OutreachDraft",
    "SequencePlan",
    "ThreadStrategy",
]
