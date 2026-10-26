"""``OutreachKickoffWorkflow``: the WF-02 daily catch-up (REQUIREMENTS.md INV-04, WF-02).

One run per ``outreach-kickoff`` schedule fire (daily, after discovery has had its day). The
workflow is a thin shell: every rule lives in ``activities/sequence_starter.py``
(``run_outreach_kickoff``): qualified investors with a primary email in every Active / Watch
market, priority tier first, skipping suppressed contacts and investors already on a sequence,
within the daily kickoff cap. ``run_a2`` starts sequences for freshly researched investors on
its own; this run picks up whatever that missed (cap reached, A2 ran before the market was
promoted, a start that failed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.sequence_starter import (
        OutreachKickoffInput,
        OutreachKickoffResult,
        run_outreach_kickoff,
    )

WORKFLOW_NAME = "OutreachKickoffWorkflow"
KICKOFF_ACTIVITY_TIMEOUT = timedelta(minutes=10)
KICKOFF_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)


@dataclass(slots=True)
class OutreachKickoffWorkflowInput:
    market_id: str | None = None  # None = every Active / Watch market
    limit_per_market: int | None = None


@workflow.defn(name=WORKFLOW_NAME)
class OutreachKickoffWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: OutreachKickoffResult | None = None

    @workflow.run
    async def run(self, input: OutreachKickoffWorkflowInput) -> OutreachKickoffResult:
        self._state = "starting_sequences"
        self._result = await workflow.execute_activity(
            run_outreach_kickoff,
            OutreachKickoffInput(
                market_id=input.market_id, limit_per_market=input.limit_per_market
            ),
            start_to_close_timeout=KICKOFF_ACTIVITY_TIMEOUT,
            retry_policy=KICKOFF_RETRY,
        )
        self._state = "done"
        return self._result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "result": self._result.to_json() if self._result else None,
        }


__all__ = [
    "KICKOFF_RETRY",
    "WORKFLOW_NAME",
    "OutreachKickoffWorkflow",
    "OutreachKickoffWorkflowInput",
]
