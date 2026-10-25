"""Graph maintenance and inbound workflows (REQUIREMENTS.md WF-11, WF-12, WF-19 step 1, WF-23).

- ``GraphSubscriptionRenewalWorkflow``: one run per Temporal Schedule fire (every 48 h, WF-11).
  Renews/creates the inbox subscription; after two consecutive failed runs it raises an alert
  (structured log + audit event). The failure streak lives in the audit log, not in workflow
  state, so scheduled runs need no memory of each other.
- ``GraphDeltaPollWorkflow``: one run per fire (every 15 min, WF-12). Reconciles the last
  lookback window through the same idempotent upsert the webhook path uses.
- ``GraphInboundMessageWorkflow``: started by the webhook per notification, workflow id derived
  from (subscription id, message id) so a duplicate notification is a no-op (XC-03). Today it
  fetches and persists the message; BO-07 extends it with A4 -> A5 -> policy.

All three take a single dataclass input (milestone-1 decision 8).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
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


@workflow.defn(name="GraphInboundMessageWorkflow")
class GraphInboundMessageWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: FetchMessageResult | None = None

    @workflow.run
    async def run(self, input: InboundMessageInput) -> FetchMessageResult:
        self._state = "fetching"
        self._result = await workflow.execute_activity(
            fetch_message,
            FetchMessageInput(
                graph_id=input.graph_id,
                subscription_id=input.subscription_id,
                change_type=input.change_type,
            ),
            start_to_close_timeout=GRAPH_ACTIVITY_TIMEOUT,
            retry_policy=GRAPH_RETRY,
        )
        # BO-07: classify (A4) -> draft -> QA (A5) -> policy -> send/queue, and cancel the
        # sequence on a reply (WF-19), all from here.
        self._state = "done"
        return self._result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


def inbound_workflow_id(subscription_id: str, resource_id: str) -> str:
    """Deterministic workflow id for one notification: the webhook's idempotency key (XC-03)."""
    digest = hashlib.sha256(f"{subscription_id}:{resource_id}".encode()).hexdigest()[:32]
    return f"graph-inbound-{digest}"


__all__ = [
    "RENEWAL_ALERT_AFTER_FAILURES",
    "DeltaPollWorkflowInput",
    "GraphDeltaPollWorkflow",
    "GraphInboundMessageWorkflow",
    "GraphSubscriptionRenewalWorkflow",
    "InboundMessageInput",
    "SubscriptionRenewalInput",
    "SubscriptionRenewalOutcome",
    "inbound_workflow_id",
]
