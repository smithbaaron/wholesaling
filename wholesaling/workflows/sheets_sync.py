"""``SheetsSyncWorkflow`` (REQUIREMENTS.md WF-13, INT-14, STK-02).

One run per Temporal Schedule fire (``workflows/schedules.py``, ``sheets-sync``, every 15 min,
overlap ``skip``). The workflow is a thin shell around ``activities.sheets.sync_projection``:
Postgres -> Sheets, one way, every tab unless the input narrows it. Partial failures are
reported in the result (and the ``sheets.sync`` audit event), never raised, so a broken tab does
not fail the schedule; the next fire retries it through the idempotent upsert.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.sheets import (
        SyncProjectionInput,
        SyncProjectionResult,
        sync_projection,
    )

SYNC_ACTIVITY_TIMEOUT = timedelta(minutes=10)
SYNC_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)


@dataclass(slots=True)
class SheetsSyncInput:
    tabs: list[str] | None = None  # None = every tab in TAB_ORDER


@workflow.defn(name="SheetsSyncWorkflow")
class SheetsSyncWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: SyncProjectionResult | None = None

    @workflow.run
    async def run(self, input: SheetsSyncInput) -> SyncProjectionResult:
        self._state = "syncing"
        result: SyncProjectionResult = await workflow.execute_activity(
            sync_projection,
            SyncProjectionInput(
                tabs=list(input.tabs) if input.tabs else None,
                idempotency_key=f"sheets-sync:{workflow.info().workflow_id}",
            ),
            start_to_close_timeout=SYNC_ACTIVITY_TIMEOUT,
            retry_policy=SYNC_RETRY,
        )
        self._result = result
        self._state = "done" if result.ok else "done_with_failures"
        return result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


__all__ = ["SYNC_RETRY", "SheetsSyncInput", "SheetsSyncWorkflow"]
