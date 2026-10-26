"""``LearningStatsWorkflow`` (REQUIREMENTS.md BO-13 "learning loop on outcomes", M1-05).

One run per Temporal Schedule fire (``workflows/schedules.py``, ``learning-stats``, daily). A
thin shell around ``activities.learning.compute_learning_stats``, which persists the day's
``learning.stats`` audit event.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.learning import (
        DEFAULT_WINDOW_DAYS,
        LearningInput,
        LearningResult,
        compute_learning_stats,
    )

LEARNING_ACTIVITY_TIMEOUT = timedelta(minutes=5)
LEARNING_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
)


@dataclass(slots=True)
class LearningStatsInput:
    day: str | None = None  # YYYY-MM-DD in OPERATOR_TIMEZONE; None = the current local day
    window_days: int = DEFAULT_WINDOW_DAYS


@workflow.defn(name="LearningStatsWorkflow")
class LearningStatsWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: LearningResult | None = None

    @workflow.run
    async def run(self, input: LearningStatsInput) -> LearningResult:
        self._state = "computing"
        result: LearningResult = await workflow.execute_activity(
            compute_learning_stats,
            LearningInput(
                day=input.day,
                window_days=input.window_days,
                idempotency_key=(f"learning:{input.day or 'today'}:{workflow.info().workflow_id}"),
            ),
            start_to_close_timeout=LEARNING_ACTIVITY_TIMEOUT,
            retry_policy=LEARNING_RETRY,
        )
        self._result = result
        self._state = "done"
        return result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


__all__ = ["LEARNING_RETRY", "LearningStatsInput", "LearningStatsWorkflow"]
