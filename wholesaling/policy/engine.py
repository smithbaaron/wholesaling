"""The autonomy policy engine (REQUIREMENTS.md XC-01, POL-01..06, RUL-03).

Every outbound action runs the same code path; this engine decides the *last* step:

- ``send``             the action proceeds automatically
- ``queue_for_human``  the draft/offer waits in the review queue
- ``blocked``          the action must not happen (QA block, suppression, OFF behaviour)

It reads the live level per action from the ``policies`` table, looks up the behaviour in
``policy.actions.POLICY_TABLE`` and evaluates the row's condition against a ``PolicyContext``
assembled by the caller. The engine never sends anything itself and never calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import PolicyDecision, QaVerdict
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.policy.actions import (
    HARD_FLOOR_ACTIONS,
    POLICY_TABLE,
    AutonomyLevel,
    Behaviour,
    PolicyAction,
)

# OPN-10 default: QA confidence needed for an L1 auto-reply. Overridable per action via
# ``policies.threshold``.
DEFAULT_QA_CONFIDENCE_THRESHOLD = Decimal("0.85")


@dataclass(slots=True)
class PolicyContext:
    """Everything a condition in POLICY_TABLE may need. Callers fill what they know.

    Unknown facts default to the *conservative* value, so a caller that forgets to fill a field
    gets ``queue_for_human``, never ``send``.
    """

    qa_verdict: QaVerdict | None = None
    qa_confidence: Decimal | None = None
    qa_flags: list[str] = field(default_factory=list)
    recipient_suppressed: bool = False
    sends_today: int | None = None  # for daily caps; None = unknown
    initial_was_approved: bool | None = None  # investor follow-ups
    sensitive_content: bool | None = None  # price / contract / legal content in a reply
    a7_gates_passed: bool | None = None  # offers
    within_negotiation_room: bool | None = None  # counters
    deal_approved: bool | None = None  # disposition blasts
    consent_recorded: bool | None = None  # SMS
    now: datetime | None = None


@dataclass(slots=True, frozen=True)
class Decision:
    action: PolicyAction
    level: AutonomyLevel
    behaviour: Behaviour
    decision: PolicyDecision
    reason: str

    @property
    def sends(self) -> bool:
        return self.decision is PolicyDecision.SEND


class PolicyEngine:
    """Decide send / queue_for_human / blocked for one action in one context."""

    def __init__(self, session: AsyncSession) -> None:
        self._policies = PolicyRepository(session)

    async def decide(self, action: PolicyAction, ctx: PolicyContext) -> Decision:
        level = await self._policies.get_level(action)
        row = await self._policies.get(action)
        daily_cap = row.daily_cap if row is not None else None
        threshold = (
            row.threshold
            if row is not None and row.threshold is not None
            else DEFAULT_QA_CONFIDENCE_THRESHOLD
        )
        return evaluate(action, level, ctx, daily_cap=daily_cap, qa_threshold=threshold)


def evaluate(
    action: PolicyAction,
    level: AutonomyLevel,
    ctx: PolicyContext,
    *,
    daily_cap: int | None = None,
    qa_threshold: Decimal = DEFAULT_QA_CONFIDENCE_THRESHOLD,
) -> Decision:
    """Pure decision function; ``PolicyEngine.decide`` is the DB-backed wrapper."""
    behaviour, condition = POLICY_TABLE[action][level]

    def _d(decision: PolicyDecision, reason: str) -> Decision:
        return Decision(action, level, behaviour, decision, reason)

    # Universal blocks come first; they apply at every level (XC-05, WF-21, INV-18).
    if action in HARD_FLOOR_ACTIONS:
        return _d(PolicyDecision.QUEUE_FOR_HUMAN, "hard floor: human always (POL-06)")
    if action is not PolicyAction.OPT_OUT_SUPPRESSION and ctx.recipient_suppressed:
        return _d(PolicyDecision.BLOCKED, "recipient is suppressed")
    if ctx.qa_verdict is QaVerdict.BLOCK:
        return _d(PolicyDecision.BLOCKED, "QA blocked the draft")

    if behaviour is Behaviour.OFF:
        return _d(PolicyDecision.BLOCKED, f"action is off at {level.value}")
    if behaviour is Behaviour.HUMAN:
        return _d(PolicyDecision.QUEUE_FOR_HUMAN, "human only at this level")
    if behaviour is Behaviour.DRAFT:
        return _d(PolicyDecision.QUEUE_FOR_HUMAN, f"draft at {level.value}: human sends")

    # AUTO and AUTO_IF both require an approved QA verdict when a QA review exists
    # (every outbound draft is QA'd, INV-17); REWRITE means a human must look.
    if ctx.qa_verdict is QaVerdict.REWRITE:
        return _d(PolicyDecision.QUEUE_FOR_HUMAN, "QA asked for a rewrite")
    if daily_cap is not None:
        if ctx.sends_today is None:
            return _d(PolicyDecision.QUEUE_FOR_HUMAN, "daily cap set but send count unknown")
        if ctx.sends_today >= daily_cap:
            return _d(PolicyDecision.QUEUE_FOR_HUMAN, f"daily cap {daily_cap} reached")

    if behaviour is Behaviour.AUTO:
        return _d(PolicyDecision.SEND, f"auto at {level.value}")

    # Behaviour.AUTO_IF: evaluate the row's condition.
    ok, why = _condition_holds(action, ctx, qa_threshold)
    if ok:
        return _d(PolicyDecision.SEND, f"condition met: {condition}")
    return _d(PolicyDecision.QUEUE_FOR_HUMAN, f"condition not met: {why}")


def _condition_holds(
    action: PolicyAction, ctx: PolicyContext, qa_threshold: Decimal
) -> tuple[bool, str]:
    match action:
        case PolicyAction.INVESTOR_INITIAL_OUTREACH | PolicyAction.SELLER_OUTREACH_EMAIL:
            # "QA-approved; daily cap" / "daily cap" — cap handled above; need QA approval.
            if ctx.qa_verdict is QaVerdict.APPROVE:
                return True, ""
            return False, "no QA approval"
        case PolicyAction.INVESTOR_FOLLOWUP:
            if ctx.initial_was_approved is True and ctx.qa_verdict is QaVerdict.APPROVE:
                return True, ""
            return False, "initial not approved or follow-up not QA-approved"
        case PolicyAction.INVESTOR_REPLY:
            if ctx.qa_verdict is not QaVerdict.APPROVE:
                return False, "no QA approval"
            if ctx.qa_confidence is None or ctx.qa_confidence < qa_threshold:
                return False, f"QA confidence below {qa_threshold}"
            if ctx.sensitive_content is not False:
                return False, "price/contract/legal content or unknown"
            return True, ""
        case PolicyAction.OFFER_SEND:
            if ctx.a7_gates_passed is True and ctx.qa_verdict is QaVerdict.APPROVE:
                return True, ""
            return False, "A7 gates not all passed or no QA approval"
        case PolicyAction.COUNTER_ACCEPTANCE:
            if ctx.within_negotiation_room is True:
                return True, ""
            return False, "counter outside negotiation room or unknown"
        case PolicyAction.BUYER_DISPOSITION_BLAST:
            if ctx.deal_approved is True and ctx.qa_verdict is QaVerdict.APPROVE:
                return True, ""
            return False, "deal not approved or no QA approval"
        case PolicyAction.SELLER_OUTREACH_SMS:
            if ctx.consent_recorded is True:
                return True, ""
            return False, "no consent record"
        case _:  # pragma: no cover - every AUTO_IF row is listed above
            return False, "no condition evaluator"
