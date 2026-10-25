"""``DailyDigestWorkflow`` (REQUIREMENTS.md BO-07 "daily digest", BO-21, INT-18).

One run per Temporal Schedule fire. The schedule (``workflows/schedules.py``,
``daily-digest``) fires once a day at ``DIGEST_HOUR_LOCAL`` in ``OPERATOR_TIMEZONE``
(defaults 07:00 America/Chicago). WF-18's fixed 07:00 *review* time was rejected: the digest
is a summary, the review queue itself is never time-gated.

The workflow is a thin shell: all I/O is ``activities.digest.compute_and_send_digest``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.digest import DigestInput, DigestResult, compute_and_send_digest

DIGEST_ACTIVITY_TIMEOUT = timedelta(minutes=5)
DIGEST_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=5,
)


@dataclass(slots=True)
class DailyDigestInput:
    day: str | None = None  # YYYY-MM-DD in OPERATOR_TIMEZONE; None = the current local day


@workflow.defn(name="DailyDigestWorkflow")
class DailyDigestWorkflow:
    def __init__(self) -> None:
        self._state = "pending"
        self._result: DigestResult | None = None

    @workflow.run
    async def run(self, input: DailyDigestInput) -> DigestResult:
        self._state = "computing"
        result: DigestResult = await workflow.execute_activity(
            compute_and_send_digest,
            DigestInput(
                day=input.day,
                idempotency_key=f"digest:{input.day or 'today'}:{workflow.info().workflow_id}",
            ),
            start_to_close_timeout=DIGEST_ACTIVITY_TIMEOUT,
            retry_policy=DIGEST_RETRY,
        )
        self._result = result
        self._state = "done" if not result.failed_sinks else "done_with_failures"
        return result

    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"state": self._state, "result": asdict(self._result) if self._result else None}


__all__ = ["DIGEST_RETRY", "DailyDigestInput", "DailyDigestWorkflow"]
