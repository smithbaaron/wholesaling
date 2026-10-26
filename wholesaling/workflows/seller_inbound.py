"""``SellerInboundWorkflow``: the seller channel's inbound pipeline (REQUIREMENTS.md SEL-06,
SEL-07, SEL-08, WF-19, WF-21, POL-03f, POL-03l, XC-01, XC-03).

Mirrors ``GraphInboundMessageWorkflow`` for the seller side::

    email:    fetch_message (one row per internetMessageId)          [fetching]
    web form: the route already wrote the ``messages`` row (channel web_form)
      -> bounce?  seller_notify_bounce -> signal ``bounce`` to the seller sequence, stop
      -> seller_classify_inbound   S4 classify (reply / not_interested / opt_out / unrelated);
                                   opt-out -> suppression (auto at every level) + signal;
                                   reply -> signal ``reply``                        [classifying]
      -> reply?  seller_intake     S4 extract -> intake merge -> parcel check ->
                                   complete: audit intake.completed, start OfferWorkflow
                                             (``offer:{intake_id}``), signal intake_complete
                                   else: S4 reply (<= 2 asks, photo link) -> A5 QA ->
                                         PolicyEngine(seller_intake_reply) -> send | queue |
                                         blocked                                   [intake]
      -> queued?  wait for ``human_decision`` (14 d) -> seller_apply_human_decision
                                                                                [awaiting_human]

Workflow ids: ``seller_inbound_workflow_id("email", graph_id)`` from a Graph notification,
``seller_inbound_workflow_id("form", submission_id)`` from ``POST /sell`` (SEL-12: the
submission id is the idempotency key, so a re-POST reuses the running workflow).

Routing note: today ``POST /webhooks/graph`` starts ``GraphInboundMessageWorkflow`` for every
notification. This workflow is self-sufficient (an inbound message with no seller lead behind
its thread or sender ends with ``skipped_reason="not_seller"``), so the webhook owner can route
by thread once both pipelines run side by side; until then the synthetic seller pilot and the
web form are its callers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from wholesaling.workflows.graph_maintenance import HumanDecision

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.graph import FetchMessageInput, FetchMessageResult, fetch_message
    from wholesaling.activities.seller import (
        SELLER_INBOUND_WORKFLOW_NAME,
        SOURCE_EMAIL,
        SOURCE_WEB_FORM,
        SellerBounceInput,
        SellerClassifyInput,
        SellerClassifyResult,
        SellerHumanDecisionInput,
        SellerHumanDecisionResult,
        SellerIntakeInput,
        SellerIntakeResult,
        seller_apply_human_decision,
        seller_classify_inbound,
        seller_inbound_workflow_id,
        seller_intake,
        seller_notify_bounce,
    )

WORKFLOW_NAME = SELLER_INBOUND_WORKFLOW_NAME
GRAPH_ACTIVITY_TIMEOUT = timedelta(minutes=2)
INTAKE_ACTIVITY_TIMEOUT = timedelta(minutes=5)
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
HUMAN_DECISION_TIMEOUT = timedelta(days=14)
HUMAN_DECISION_SIGNAL = "human_decision"


@dataclass(slots=True)
class SellerInboundInput:
    """``source="email"`` needs ``graph_id``; ``source="web_form"`` needs ``message_id`` (the
    form's ``messages`` row the route created)."""

    source: str = SOURCE_EMAIL
    graph_id: str | None = None
    subscription_id: str | None = None
    change_type: str | None = None
    message_id: str | None = None
    submission_id: str | None = None


@dataclass(slots=True)
class SellerInboundResult:
    found: bool
    source: str = SOURCE_EMAIL
    created: bool = False
    message_id: str | None = None
    thread_id: str | None = None
    external_message_id: str | None = None
    is_bounce: bool = False
    stage: str = "done"
    skipped_reason: str | None = None
    classification: str | None = None
    confidence: float | None = None
    is_auto_reply: bool = False
    asks_offer: bool = False
    signalled: str | None = None
    sequence_workflow_id: str | None = None
    seller_lead_id: str | None = None
    intake_id: str | None = None
    missing_required: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    intake_complete: bool = False
    offer_workflow_id: str | None = None
    signalled_intake_complete: bool = False
    photo_link_sent: bool = False
    draft_message_id: str | None = None
    qa_verdict: str | None = None
    qa_flags: list[str] = field(default_factory=list)
    policy_decision: str | None = None
    policy_reason: str | None = None
    sent: bool = False
    human_decision: str | None = None  # approved | rejected | timeout
    edited_by_human: bool = False


@workflow.defn(name=WORKFLOW_NAME)
class SellerInboundWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: SellerInboundResult | None = None
        self._human_decision: HumanDecision | None = None

    @workflow.run
    async def run(self, input: SellerInboundInput) -> SellerInboundResult:
        workflow_id = workflow.info().workflow_id
        result = SellerInboundResult(found=False, source=input.source)
        self._result = result

        if input.source == SOURCE_WEB_FORM:
            if not input.message_id:
                return self._finish(result, skipped_reason="no_message_id")
            result.found = True
            result.message_id = input.message_id
            message_id = input.message_id
        else:
            if not input.graph_id:
                return self._finish(result, skipped_reason="no_graph_id")
            self._state = "fetching"
            fetched: FetchMessageResult = await workflow.execute_activity(
                fetch_message,
                FetchMessageInput(
                    graph_id=input.graph_id,
                    subscription_id=input.subscription_id,
                    change_type=input.change_type,
                    workflow_id=workflow_id,
                ),
                start_to_close_timeout=GRAPH_ACTIVITY_TIMEOUT,
                retry_policy=RETRY,
            )
            result.found = fetched.found
            result.created = fetched.created
            result.message_id = fetched.message_id
            result.thread_id = fetched.thread_id
            result.external_message_id = fetched.external_message_id
            result.is_bounce = fetched.is_bounce
            if not fetched.found or fetched.message_id is None:
                return self._finish(result, skipped_reason="not_found")
            message_id = fetched.message_id
            if fetched.is_bounce:
                self._state = "notifying_bounce"
                await workflow.execute_activity(
                    seller_notify_bounce,
                    SellerBounceInput(
                        message_id=message_id, bounced_address=fetched.bounced_address
                    ),
                    start_to_close_timeout=INTAKE_ACTIVITY_TIMEOUT,
                    retry_policy=RETRY,
                )
                return self._finish(result, skipped_reason="bounce")

        self._state = "classifying"
        classified: SellerClassifyResult = await workflow.execute_activity(
            seller_classify_inbound,
            SellerClassifyInput(message_id=message_id),
            start_to_close_timeout=INTAKE_ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        result.classification = classified.classification
        result.confidence = classified.confidence
        result.is_auto_reply = classified.is_auto_reply
        result.asks_offer = classified.asks_offer
        result.signalled = classified.signalled
        result.sequence_workflow_id = classified.sequence_workflow_id
        result.seller_lead_id = classified.seller_lead_id
        if classified.skipped_reason is not None or classified.classification is None:
            return self._finish(result, skipped_reason=classified.skipped_reason)
        if not classified.warrants_intake:
            return self._finish(result, skipped_reason=f"no_intake:{classified.classification}")

        self._state = "intake"
        intake: SellerIntakeResult = await workflow.execute_activity(
            seller_intake,
            SellerIntakeInput(message_id=message_id, workflow_id=workflow_id),
            start_to_close_timeout=INTAKE_ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        result.intake_id = intake.intake_id
        result.missing_required = list(intake.missing_required)
        result.asked = list(intake.asked)
        result.intake_complete = intake.intake_complete
        result.offer_workflow_id = intake.offer_workflow_id
        result.signalled_intake_complete = intake.signalled_intake_complete
        result.photo_link_sent = intake.photo_link_sent
        result.draft_message_id = intake.draft_message_id
        result.qa_verdict = intake.qa_verdict
        result.qa_flags = list(intake.qa_flags)
        result.policy_decision = intake.policy_decision
        result.policy_reason = intake.policy_reason
        result.sent = intake.sent
        if intake.skipped_reason is not None:
            return self._finish(result, skipped_reason=intake.skipped_reason)
        if intake.intake_complete or intake.policy_decision != "queue_for_human" or intake.sent:
            return self._finish(result)

        self._state = "awaiting_human"
        try:
            await workflow.wait_condition(
                lambda: self._human_decision is not None, timeout=HUMAN_DECISION_TIMEOUT
            )
        except TimeoutError:
            return self._finish(result, human_decision="timeout")
        decision = self._human_decision
        assert decision is not None
        self._state = "applying_human_decision"
        applied: SellerHumanDecisionResult = await workflow.execute_activity(
            seller_apply_human_decision,
            SellerHumanDecisionInput(
                message_id=message_id,
                approved=decision.approved,
                edited_body=decision.edited_body,
                decided_by=decision.actor_id,
            ),
            start_to_close_timeout=INTAKE_ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        result.sent = applied.sent
        result.edited_by_human = applied.edited
        if applied.draft_message_id:
            result.draft_message_id = applied.draft_message_id
        return self._finish(result, human_decision="approved" if decision.approved else "rejected")

    def _finish(
        self,
        result: SellerInboundResult,
        *,
        skipped_reason: str | None = None,
        human_decision: str | None = None,
    ) -> SellerInboundResult:
        result.stage = "done"
        if skipped_reason is not None:
            result.skipped_reason = skipped_reason
        if human_decision is not None:
            result.human_decision = human_decision
        self._state = "done"
        self._result = result
        return result

    @workflow.signal(name=HUMAN_DECISION_SIGNAL)
    async def human_decision(self, decision: HumanDecision) -> None:
        if self._human_decision is None:
            self._human_decision = decision

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


__all__ = [
    "HUMAN_DECISION_SIGNAL",
    "HUMAN_DECISION_TIMEOUT",
    "WORKFLOW_NAME",
    "HumanDecision",
    "SellerInboundInput",
    "SellerInboundResult",
    "SellerInboundWorkflow",
    "seller_inbound_workflow_id",
]
