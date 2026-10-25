"""Graph maintenance and inbound workflows (REQUIREMENTS.md WF-11, WF-12, WF-19 step 1, WF-23).

- ``GraphSubscriptionRenewalWorkflow``: one run per Temporal Schedule fire (every 48 h, WF-11).
  Renews/creates the inbox subscription; after two consecutive failed runs it raises an alert
  (structured log + audit event). The failure streak lives in the audit log, not in workflow
  state, so scheduled runs need no memory of each other.
- ``GraphDeltaPollWorkflow``: one run per fire (every 15 min, WF-12). Reconciles the last
  lookback window through the same idempotent upsert the webhook path uses.
- ``GraphInboundMessageWorkflow``: started by the webhook per notification, workflow id derived
  from (subscription id, message id) so a duplicate notification is a no-op (XC-03). Fetches and
  persists the message, then (BO-07, WF-19) runs the inbound pipeline: A4 classify -> buy-box
  extraction -> A4 draft -> A5 QA -> policy -> send, or wait for the review queue's
  ``human_decision`` signal when the policy queued the draft. See docs/agents/conversation.md.

All three take a single dataclass input (milestone-1 decision 8).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.conversation import (
        ApplyHumanDecisionInput,
        ApplyHumanDecisionResult,
        ClassifyInboundInput,
        ClassifyInboundResult,
        DraftReplyInput,
        DraftReplyResult,
        ExtractBuyBoxInput,
        ExtractBuyBoxResult,
        NotifyBounceInput,
        apply_human_decision,
        classify_inbound,
        draft_and_qa_reply,
        extract_and_upsert_buy_box,
        notify_bounce,
    )
    from wholesaling.activities.graph import (
        DeltaPollInput,
        DeltaPollResult,
        FetchMessageInput,
        FetchMessageResult,
        RaiseAlertInput,
        RecordRenewalOutcomeInput,
        RecordRenewalOutcomeResult,
        RenewSubscriptionsInput,
        RenewSubscriptionsResult,
        delta_poll,
        fetch_message,
        raise_alert,
        record_renewal_outcome,
        renew_subscriptions,
    )

GRAPH_ACTIVITY_TIMEOUT = timedelta(minutes=2)
DELTA_ACTIVITY_TIMEOUT = timedelta(minutes=10)
GRAPH_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)
RENEWAL_ALERT_AFTER_FAILURES = 2
# Model calls (A4/A5) sit inside these activities: generous timeout, same retry policy.
CONVERSATION_ACTIVITY_TIMEOUT = timedelta(minutes=5)
# How long a queued draft waits for the review queue before the workflow stops waiting. The
# draft stays queued (``policy_decision=queue_for_human``) and the review queue can still act on
# it through ``apply_human_decision``; the workflow simply no longer holds the slot open.
HUMAN_DECISION_TIMEOUT = timedelta(days=14)
HUMAN_DECISION_SIGNAL = "human_decision"


@dataclass(slots=True)
class SubscriptionRenewalInput:
    notification_url: str | None = None
    resource: str | None = None
    lifetime_hours: int = 70


@dataclass(slots=True)
class SubscriptionRenewalOutcome:
    ok: bool
    consecutive_failures: int
    alerted: bool
    renewed: list[str]
    created: list[str]
    error: str | None = None


@workflow.defn(name="GraphSubscriptionRenewalWorkflow")
class GraphSubscriptionRenewalWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._outcome: SubscriptionRenewalOutcome | None = None

    @workflow.run
    async def run(self, input: SubscriptionRenewalInput) -> SubscriptionRenewalOutcome:
        self._state = "renewing"
        renewed: list[str] = []
        created: list[str] = []
        error: str | None = None
        ok = True
        try:
            result: RenewSubscriptionsResult = await workflow.execute_activity(
                renew_subscriptions,
                RenewSubscriptionsInput(
                    notification_url=input.notification_url,
                    resource=input.resource,
                    lifetime_hours=input.lifetime_hours,
                ),
                start_to_close_timeout=GRAPH_ACTIVITY_TIMEOUT,
                retry_policy=GRAPH_RETRY,
            )
            renewed, created = list(result.renewed), list(result.created)
        except ActivityError as exc:
            ok = False
            error = str(exc.cause or exc)
            workflow.logger.warning("graph_renewal.failed", extra={"error": error})

        bookkeeping: RecordRenewalOutcomeResult = await workflow.execute_activity(
            record_renewal_outcome,
            RecordRenewalOutcomeInput(
                ok=ok, detail=error or f"renewed={renewed} created={created}"
            ),
            start_to_close_timeout=GRAPH_ACTIVITY_TIMEOUT,
            retry_policy=GRAPH_RETRY,
        )
        alerted = False
        if not ok and bookkeeping.consecutive_failures >= RENEWAL_ALERT_AFTER_FAILURES:
            self._state = "alerting"
            alerted = await workflow.execute_activity(
                raise_alert,
                RaiseAlertInput(
                    kind="graph.subscription_renewal_failed",
                    detail=(
                        f"Graph subscription renewal failed {bookkeeping.consecutive_failures} "
                        f"times in a row: {error}"
                    ),
                    payload={"consecutive_failures": bookkeeping.consecutive_failures},
                    idempotency_key=f"alert:graph-renewal:{workflow.info().workflow_id}",
                ),
                start_to_close_timeout=GRAPH_ACTIVITY_TIMEOUT,
                retry_policy=GRAPH_RETRY,
            )
        self._state = "done"
        self._outcome = SubscriptionRenewalOutcome(
            ok=ok,
            consecutive_failures=bookkeeping.consecutive_failures,
            alerted=alerted,
            renewed=renewed,
            created=created,
            error=error,
        )
        return self._outcome

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "outcome": asdict(self._outcome) if self._outcome else None}


@dataclass(slots=True)
class DeltaPollWorkflowInput:
    folder: str = "inbox"
    delta_token: str | None = None
    lookback_hours: int = 24


@workflow.defn(name="GraphDeltaPollWorkflow")
class GraphDeltaPollWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: DeltaPollResult | None = None

    @workflow.run
    async def run(self, input: DeltaPollWorkflowInput) -> DeltaPollResult:
        self._state = "polling"
        self._result = await workflow.execute_activity(
            delta_poll,
            DeltaPollInput(
                folder=input.folder,
                delta_token=input.delta_token,
                lookback_hours=input.lookback_hours,
            ),
            start_to_close_timeout=DELTA_ACTIVITY_TIMEOUT,
            retry_policy=GRAPH_RETRY,
        )
        self._state = "done"
        return self._result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


@dataclass(slots=True)
class InboundMessageInput:
    graph_id: str
    subscription_id: str | None = None
    change_type: str | None = None


@dataclass(slots=True)
class HumanDecision:
    """Payload of the ``human_decision`` signal the review queue sends to a waiting workflow.

    Field names match ``wholesaling.db.repositories.review.HumanDecision`` (the sender's
    dataclass; Temporal's converter maps by field name). ``approved=True`` sends the queued draft
    (with ``edited_body`` replacing the body when the reviewer changed it); ``approved=False``
    discards it. ``step`` belongs to sequence drafts and is ignored here; ``message_id`` is the
    queued draft's row id (informational: the workflow already knows its inbound message);
    ``actor_id`` is the operator for the audit row.
    """

    approved: bool
    edited_body: str | None = None
    step: int | None = None
    message_id: str | None = None
    actor_id: str | None = None


@dataclass(slots=True)
class InboundPipelineResult:
    """What one inbound notification produced. The fetch fields are flattened so callers that
    only care about persistence keep reading ``found`` / ``created``."""

    found: bool
    created: bool = False
    message_id: str | None = None
    thread_id: str | None = None
    external_message_id: str | None = None
    is_bounce: bool = False
    bounced_address: str | None = None
    suppression_created: bool = False
    stage: str = "done"
    skipped_reason: str | None = None
    classification: str | None = None
    confidence: float | None = None
    is_auto_reply: bool = False
    reconnect_date: str | None = None
    signalled: str | None = None
    sequence_workflow_id: str | None = None
    buy_box_versions: list[dict[str, Any]] = field(default_factory=list)
    draft_message_id: str | None = None
    qa_verdict: str | None = None
    qa_confidence: float | None = None
    qa_flags: list[str] = field(default_factory=list)
    policy_decision: str | None = None
    policy_reason: str | None = None
    sent: bool = False
    human_decision: str | None = None  # approved | rejected | timeout
    edited_by_human: bool = False


@workflow.defn(name="GraphInboundMessageWorkflow")
class GraphInboundMessageWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: InboundPipelineResult | None = None
        self._human_decision: HumanDecision | None = None

    @workflow.run
    async def run(self, input: InboundMessageInput) -> InboundPipelineResult:
        self._state = "fetching"
        fetched: FetchMessageResult = await workflow.execute_activity(
            fetch_message,
            FetchMessageInput(
                graph_id=input.graph_id,
                subscription_id=input.subscription_id,
                change_type=input.change_type,
            ),
            start_to_close_timeout=GRAPH_ACTIVITY_TIMEOUT,
            retry_policy=GRAPH_RETRY,
        )
        result = InboundPipelineResult(
            found=fetched.found,
            created=fetched.created,
            message_id=fetched.message_id,
            thread_id=fetched.thread_id,
            external_message_id=fetched.external_message_id,
            is_bounce=fetched.is_bounce,
            bounced_address=fetched.bounced_address,
            suppression_created=fetched.suppression_created,
        )
        self._result = result
        if not fetched.found or fetched.message_id is None:
            return self._finish(result, "done", skipped_reason="not_found")

        # WF-19: dedupe happened in fetch_message (one row per internetMessageId); from here on
        # every activity is idempotent per message id, so a replayed workflow repeats nothing.
        if fetched.is_bounce:
            self._state = "notifying_bounce"
            await workflow.execute_activity(
                notify_bounce,
                NotifyBounceInput(
                    message_id=fetched.message_id, bounced_address=fetched.bounced_address
                ),
                start_to_close_timeout=CONVERSATION_ACTIVITY_TIMEOUT,
                retry_policy=GRAPH_RETRY,
            )
            return self._finish(result, "done", skipped_reason="bounce")

        self._state = "classifying"
        classified: ClassifyInboundResult = await workflow.execute_activity(
            classify_inbound,
            ClassifyInboundInput(message_id=fetched.message_id),
            start_to_close_timeout=CONVERSATION_ACTIVITY_TIMEOUT,
            retry_policy=GRAPH_RETRY,
        )
        result.classification = classified.classification
        result.confidence = classified.confidence
        result.is_auto_reply = classified.is_auto_reply
        result.reconnect_date = classified.reconnect_date
        result.signalled = classified.signalled
        result.sequence_workflow_id = classified.sequence_workflow_id
        result.suppression_created = result.suppression_created or classified.suppression_created
        if classified.skipped_reason is not None or classified.classification is None:
            return self._finish(result, "done", skipped_reason=classified.skipped_reason)

        classification = classified.classification
        if classified.is_auto_reply or classification in _NO_REPLY_CLASSES:
            # unrelated, not_interested, opt_out and automatic replies get no draft.
            return self._finish(result, "done", skipped_reason=f"no_reply:{classification}")

        if classification in _BUY_BOX_CLASSES:
            self._state = "extracting_buy_box"
            extracted: ExtractBuyBoxResult = await workflow.execute_activity(
                extract_and_upsert_buy_box,
                ExtractBuyBoxInput(message_id=fetched.message_id),
                start_to_close_timeout=CONVERSATION_ACTIVITY_TIMEOUT,
                retry_policy=GRAPH_RETRY,
            )
            result.buy_box_versions = [asdict(v) for v in extracted.versions]

        self._state = "drafting"
        drafted: DraftReplyResult = await workflow.execute_activity(
            draft_and_qa_reply,
            DraftReplyInput(message_id=fetched.message_id),
            start_to_close_timeout=CONVERSATION_ACTIVITY_TIMEOUT,
            retry_policy=GRAPH_RETRY,
        )
        result.draft_message_id = drafted.draft_message_id
        result.qa_verdict = drafted.qa_verdict
        result.qa_confidence = drafted.qa_confidence
        result.qa_flags = list(drafted.qa_flags)
        result.policy_decision = drafted.policy_decision
        result.policy_reason = drafted.policy_reason
        result.sent = drafted.sent
        if drafted.policy_decision != "queue_for_human" or drafted.sent:
            return self._finish(result, "done")

        self._state = "awaiting_human"
        try:
            await workflow.wait_condition(
                lambda: self._human_decision is not None, timeout=HUMAN_DECISION_TIMEOUT
            )
        except TimeoutError:
            return self._finish(result, "done", human_decision="timeout")
        decision = self._human_decision
        assert decision is not None
        self._state = "applying_human_decision"
        applied: ApplyHumanDecisionResult = await workflow.execute_activity(
            apply_human_decision,
            ApplyHumanDecisionInput(
                message_id=fetched.message_id,
                approved=decision.approved,
                edited_body=decision.edited_body,
                decided_by=decision.actor_id,
            ),
            start_to_close_timeout=CONVERSATION_ACTIVITY_TIMEOUT,
            retry_policy=GRAPH_RETRY,
        )
        result.sent = applied.sent
        result.edited_by_human = applied.edited
        if applied.draft_message_id:
            result.draft_message_id = applied.draft_message_id
        return self._finish(
            result, "done", human_decision="approved" if decision.approved else "rejected"
        )

    def _finish(
        self,
        result: InboundPipelineResult,
        stage: str,
        *,
        skipped_reason: str | None = None,
        human_decision: str | None = None,
    ) -> InboundPipelineResult:
        result.stage = stage
        if skipped_reason is not None:
            result.skipped_reason = skipped_reason
        if human_decision is not None:
            result.human_decision = human_decision
        self._state = stage
        self._result = result
        return result

    @workflow.signal(name=HUMAN_DECISION_SIGNAL)
    async def human_decision(self, decision: HumanDecision) -> None:
        """Review queue verdict for a queued draft; the first decision wins."""
        if self._human_decision is None:
            self._human_decision = decision

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


# Classes that never get a drafted reply (INV-13 handling; opt-outs are suppressed instead).
_NO_REPLY_CLASSES: frozenset[str] = frozenset({"unrelated", "not_interested", "opt_out"})
# Classes whose text may carry buy-box criteria worth extracting (INV-14).
_BUY_BOX_CLASSES: frozenset[str] = frozenset({"interested", "buy_box_supplied"})


def inbound_workflow_id(subscription_id: str, resource_id: str) -> str:
    """Deterministic workflow id for one notification: the webhook's idempotency key (XC-03)."""
    digest = hashlib.sha256(f"{subscription_id}:{resource_id}".encode()).hexdigest()[:32]
    return f"graph-inbound-{digest}"


__all__ = [
    "HUMAN_DECISION_SIGNAL",
    "HUMAN_DECISION_TIMEOUT",
    "RENEWAL_ALERT_AFTER_FAILURES",
    "DeltaPollWorkflowInput",
    "GraphDeltaPollWorkflow",
    "GraphInboundMessageWorkflow",
    "GraphSubscriptionRenewalWorkflow",
    "HumanDecision",
    "InboundMessageInput",
    "InboundPipelineResult",
    "SubscriptionRenewalInput",
    "SubscriptionRenewalOutcome",
    "inbound_workflow_id",
]
