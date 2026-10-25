"""Hello workflow: the template every later sequence workflow copies.

Pattern demonstrated here (REQUIREMENTS.md WF-01, WF-22):

- durable sleep via ``workflow.wait_condition(..., timeout=...)`` driven by ``workflow.now()``,
  so a worker restart resumes mid-wait with the remaining time intact;
- ``pause`` / ``resume`` signals that interrupt the wait without losing progress;
- a ``status`` query so operators (and the review queue) can inspect state at any time;
- all I/O in activities, imported through ``workflow.unsafe.imports_passed_through()``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from wholesaling.activities.hello import hello

ACTIVITY_TIMEOUT = timedelta(seconds=30)


@dataclass(slots=True)
class HelloStatus:
    name: str
    state: str  # "waiting" | "paused" | "greeting" | "done"
    paused: bool
    remaining_seconds: float
    greeting: str | None


@workflow.defn(name="HelloWorkflow")
class HelloWorkflow:
    def __init__(self) -> None:
        self._name = ""
        self._paused = False
        self._state = "waiting"
        self._remaining = 0.0
        self._greeting: str | None = None

    @workflow.run
    async def run(self, name: str, delay_seconds: float = 0) -> str:
        self._name = name
        self._remaining = max(0.0, float(delay_seconds))
        workflow.logger.info("hello_workflow.started", extra={"delay_seconds": self._remaining})

        await self._durable_sleep()

        self._state = "greeting"
        self._greeting = await workflow.execute_activity(
            hello, name, start_to_close_timeout=ACTIVITY_TIMEOUT
        )
        self._state = "done"
        return self._greeting

    async def _durable_sleep(self) -> None:
        """Sleep for ``self._remaining`` seconds, honouring pause/resume.

        Each iteration waits either for the remaining time to elapse or for a pause signal.
        On pause the elapsed portion is subtracted, then we wait for resume. Replay-safe because
        every timestamp comes from ``workflow.now()``.
        """
        while self._remaining > 0:
            if self._paused:
                self._state = "paused"
                await workflow.wait_condition(lambda: not self._paused)
                self._state = "waiting"
                continue
            started = workflow.now()
            try:
                await workflow.wait_condition(
                    lambda: self._paused, timeout=timedelta(seconds=self._remaining)
                )
            except TimeoutError:
                self._remaining = 0.0
                return
            elapsed = (workflow.now() - started).total_seconds()
            self._remaining = max(0.0, self._remaining - elapsed)

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    @workflow.query
    def status(self) -> dict[str, Any]:
        return asdict(
            HelloStatus(
                name=self._name,
                state=self._state,
                paused=self._paused,
                remaining_seconds=self._remaining,
                greeting=self._greeting,
            )
        )
