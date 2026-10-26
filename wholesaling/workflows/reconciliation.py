"""``DailyReconciliationWorkflow`` (REQUIREMENTS.md WF-14, XC-09).

One run per Temporal Schedule fire (``daily-reconciliation``, every 24 h). The single activity,
``activities.sheets.reconcile_sources``, compares Outlook (Graph delta over the lookback
window) with ``messages`` and Postgres with every Sheets tab, then records the mismatches as an
audit event and raises an alert when there are any. It never writes to fix anything: the
operator decides (V1R-06 rejected "reconciliation from Sheets back to the database").
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.sheets import (
        DEFAULT_LOOKBACK_HOURS,
        ReconcileInput,
        ReconcileResult,
        reconcile_sources,
    )

RECONCILE_ACTIVITY_TIMEOUT = timedelta(minutes=15)
RECONCILE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)


@dataclass(slots=True)
class DailyReconciliationInput:
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    tabs: list[str] | None = None  # None = every tab


@workflow.defn(name="DailyReconciliationWorkflow")
class DailyReconciliationWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: ReconcileResult | None = None

    @workflow.run
    async def run(self, input: DailyReconciliationInput) -> ReconcileResult:
        self._state = "comparing"
        result: ReconcileResult = await workflow.execute_activity(
            reconcile_sources,
            ReconcileInput(
                lookback_hours=input.lookback_hours,
                tabs=list(input.tabs) if input.tabs else None,
                idempotency_key=f"reconciliation:{workflow.info().workflow_id}",
            ),
            start_to_close_timeout=RECONCILE_ACTIVITY_TIMEOUT,
            retry_policy=RECONCILE_RETRY,
        )
        self._result = result
        self._state = "clean" if result.ok else "mismatches"
        return result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


__all__ = ["RECONCILE_RETRY", "DailyReconciliationInput", "DailyReconciliationWorkflow"]
