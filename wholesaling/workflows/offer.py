"""``OfferWorkflow`` (REQUIREMENTS.md WF-04, A7-10, A7-11, A7-17, POL-03g).

Started by S4 when an intake is complete (``intake_complete`` -> ``offer.requested``): workflow
id ``offer:{intake_id}``, input ``OfferWorkflowInput(intake_id, seller_lead_id)``. No sleeps:
the workflow runs the verification pipeline and hands off (WF-04).

    compute_offer            steps 1-7 + draft (V1..V5 inside)      -> blocked? finish
    qa_offer_draft           A5 QA (A7-09)
    pre_send_check           V6 immediately before send, gates re-run; a changed draft is re-QA'd
    decide_and_send_offer    PolicyEngine(OFFER_SEND): send | queue_for_human | blocked
        send                 -> handoff_to_s5 (NegotiationWorkflow), done
        queue_for_human      -> wait for ``human_decision``
                                approved -> pre_send_check again (V6 is "immediately before
                                            send"); if the number changed or a gate now fails the
                                            offer is re-queued for a fresh decision; otherwise
                                            send_approved_offer -> handoff_to_s5
                                rejected -> withdraw_offer, done
        blocked              -> done (the offer row is blocked and, for V5, an alert is raised)

Signal contract (the review-queue API codes against this): ``human_decision(HumanDecision)`` with
the same dataclass as ``InvestorSequenceWorkflow`` (``step`` is unused here; ``message_id`` may
name the queued draft row). Query ``status``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from wholesaling.workflows.investor_sequence import HumanDecision

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.offers import (
        ComputeOfferInput,
        ComputeOfferResult,
        DecideOfferInput,
        DecideOfferResult,
        HandoffInput,
        HandoffResult,
        PreSendInput,
        PreSendResult,
        QaOfferInput,
        SendApprovedOfferInput,
        SendApprovedOfferResult,
        WithdrawOfferInput,
        compute_offer,
        decide_and_send_offer,
        handoff_to_s5,
        pre_send_check,
        qa_offer_draft,
        send_approved_offer,
        withdraw_offer,
    )

WORKFLOW_NAME = "OfferWorkflow"
WORKFLOW_ID_PREFIX = "offer:"
ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
MAX_HUMAN_ROUNDS = 3  # re-queue after a V6 change at most this many times, then withdraw

STATE_PENDING = "pending"
STATE_COMPUTING = "computing"
STATE_QA = "qa"
STATE_PRE_SEND = "pre_send_check"
STATE_DECIDING = "deciding"
STATE_AWAITING_HUMAN = "awaiting_human"
STATE_SENDING = "sending"
STATE_HANDOFF = "handoff"
STATE_DONE = "done"
STATE_BLOCKED = "blocked"
STATE_WITHDRAWN = "withdrawn"


def offer_workflow_id(intake_id: str) -> str:
    return f"{WORKFLOW_ID_PREFIX}{intake_id}"


@dataclass(slots=True)
class OfferWorkflowInput:
    intake_id: str
    seller_lead_id: str


@dataclass(slots=True)
class OfferWorkflowResult:
    intake_id: str
    offer_id: str | None
    outcome: str  # sent | queued_then_sent | blocked | withdrawn | human_rejected
    offer_price: str | None
    arv_confidence: str | None
    decision: str | None
    negotiation_workflow_id: str | None
    human_rounds: int
    v6_changed: bool


@dataclass(slots=True)
class OfferWorkflowStatus:
    intake_id: str
    state: str
    offer_id: str | None
    offer_price: str | None
    decision: str | None
    pending_message_id: str | None
    human_rounds: int
    last_human_decision: dict[str, Any] | None = None
    verifications: int = 0
    gates_passed: bool | None = None


@workflow.defn(name=WORKFLOW_NAME)
class OfferWorkflow:
    def __init__(self) -> None:
        self._input: OfferWorkflowInput | None = None
        self._state = STATE_PENDING
        self._offer_id: str | None = None
        self._offer_price: str | None = None
        self._confidence: str | None = None
        self._decision: str | None = None
        self._pending_message_id: str | None = None
        self._human: HumanDecision | None = None
        self._human_rounds = 0
        self._v6_changed = False
        self._verifications = 0
        self._gates_passed: bool | None = None

    async def _act(self, fn: Any, arg: Any) -> Any:
        return await workflow.execute_activity(
            fn, arg, start_to_close_timeout=ACTIVITY_TIMEOUT, retry_policy=ACTIVITY_RETRY
        )

    def _result(self, outcome: str, negotiation_wf: str | None = None) -> OfferWorkflowResult:
        assert self._input is not None
        return OfferWorkflowResult(
            intake_id=self._input.intake_id,
            offer_id=self._offer_id,
            outcome=outcome,
            offer_price=self._offer_price,
            arv_confidence=self._confidence,
            decision=self._decision,
            negotiation_workflow_id=negotiation_wf,
            human_rounds=self._human_rounds,
            v6_changed=self._v6_changed,
        )

    @workflow.run
    async def run(self, input: OfferWorkflowInput) -> OfferWorkflowResult:
        self._input = input
        wf_id = workflow.info().workflow_id

        self._state = STATE_COMPUTING
        computed: ComputeOfferResult = await self._act(
            compute_offer,
            ComputeOfferInput(
                intake_id=input.intake_id,
                seller_lead_id=input.seller_lead_id,
                idempotency_key=f"{wf_id}:compute",
            ),
        )
        self._offer_id = computed.offer_id
        self._offer_price = computed.offer_price
        self._confidence = computed.arv_confidence
        self._verifications = computed.verifications
        self._gates_passed = computed.gates_passed
        if computed.status == "blocked":
            self._state = STATE_BLOCKED
            self._decision = "blocked"
            return self._result("blocked")

        self._state = STATE_QA
        await self._act(qa_offer_draft, QaOfferInput(offer_id=computed.offer_id))

        self._state = STATE_PRE_SEND
        pre: PreSendResult = await self._act(
            pre_send_check, PreSendInput(offer_id=computed.offer_id)
        )
        self._after_pre_send(pre)
        if pre.blocked_reason:
            self._state = STATE_BLOCKED
            self._decision = "blocked"
            return self._result("blocked")
        if pre.changed:
            await self._act(qa_offer_draft, QaOfferInput(offer_id=computed.offer_id, force=True))

        self._state = STATE_DECIDING
        decided: DecideOfferResult = await self._act(
            decide_and_send_offer, DecideOfferInput(offer_id=computed.offer_id, workflow_id=wf_id)
        )
        self._decision = decided.decision
        self._pending_message_id = decided.message_id
        if decided.decision == "blocked":
            self._state = STATE_BLOCKED
            return self._result("blocked")
        if decided.decision == "send":
            return await self._handoff("sent")

        # queue_for_human: wait for the review queue (WF-04 signal human_decision).
        while True:
            self._state = STATE_AWAITING_HUMAN
            self._human = None
            await workflow.wait_condition(lambda: self._human is not None)
            human = self._human
            assert human is not None
            self._human_rounds += 1
            if not human.approved:
                self._state = STATE_WITHDRAWN
                await self._act(
                    withdraw_offer,
                    WithdrawOfferInput(
                        offer_id=computed.offer_id,
                        reason="rejected by human in review queue",
                        actor_id=human.actor_id,
                    ),
                )
                self._decision = "human_rejected"
                return self._result("human_rejected")

            # V6 is "immediately before send": run it again now that a human is sending.
            self._state = STATE_PRE_SEND
            pre = await self._act(pre_send_check, PreSendInput(offer_id=computed.offer_id))
            self._after_pre_send(pre)
            if pre.blocked_reason:
                self._state = STATE_BLOCKED
                self._decision = "blocked"
                return self._result("blocked")
            if pre.changed:
                # The number moved since the human looked: a fresh human decision is needed.
                if self._human_rounds >= MAX_HUMAN_ROUNDS:
                    await self._act(
                        withdraw_offer,
                        WithdrawOfferInput(
                            offer_id=computed.offer_id,
                            reason="offer changed after approval too many times (V6)",
                        ),
                    )
                    self._decision = "withdrawn"
                    self._state = STATE_WITHDRAWN
                    return self._result("withdrawn")
                await self._act(
                    qa_offer_draft, QaOfferInput(offer_id=computed.offer_id, force=True)
                )
                continue

            self._state = STATE_SENDING
            approved: SendApprovedOfferResult = await self._act(
                send_approved_offer,
                SendApprovedOfferInput(
                    offer_id=computed.offer_id,
                    actor_id=human.actor_id,
                    edited_body=human.edited_body,
                ),
            )
            if approved.blocked_reason:
                self._state = STATE_BLOCKED
                self._decision = f"blocked:{approved.blocked_reason}"
                return self._result("blocked")
            self._decision = "send"
            return await self._handoff("queued_then_sent")

    def _after_pre_send(self, pre: PreSendResult) -> None:
        if pre.changed:
            self._v6_changed = True
        self._offer_price = pre.offer_price
        self._gates_passed = pre.gates_passed
        self._verifications += 1

    async def _handoff(self, outcome: str) -> OfferWorkflowResult:
        assert self._input is not None and self._offer_id is not None
        self._state = STATE_HANDOFF
        handed: HandoffResult = await self._act(
            handoff_to_s5,
            HandoffInput(offer_id=self._offer_id, seller_lead_id=self._input.seller_lead_id),
        )
        self._state = STATE_DONE
        return self._result(outcome, handed.workflow_id)

    # -- signals / queries -----------------------------------------------------------------------

    @workflow.signal
    def human_decision(self, decision: HumanDecision) -> None:
        """Review-queue outcome for the queued offer. Latest wins until acted on."""
        if self._state in (STATE_DONE, STATE_BLOCKED, STATE_WITHDRAWN):
            return
        self._human = decision

    @workflow.query
    def status(self) -> dict[str, Any]:
        return asdict(
            OfferWorkflowStatus(
                intake_id=self._input.intake_id if self._input else "",
                state=self._state,
                offer_id=self._offer_id,
                offer_price=self._offer_price,
                decision=self._decision,
                pending_message_id=(
                    self._pending_message_id if self._state == STATE_AWAITING_HUMAN else None
                ),
                human_rounds=self._human_rounds,
                last_human_decision=asdict(self._human) if self._human else None,
                verifications=self._verifications,
                gates_passed=self._gates_passed,
            )
        )


__all__ = [
    "ACTIVITY_RETRY",
    "ACTIVITY_TIMEOUT",
    "MAX_HUMAN_ROUNDS",
    "WORKFLOW_ID_PREFIX",
    "WORKFLOW_NAME",
    "HumanDecision",
    "OfferWorkflow",
    "OfferWorkflowInput",
    "OfferWorkflowResult",
    "OfferWorkflowStatus",
    "offer_workflow_id",
]
