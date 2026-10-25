"""What the application needs from Temporal, kept deliberately narrow.

Workers use the raw ``temporalio.client.Client`` (they need the full SDK surface); everything
else (API, schedules, tests) talks to this Protocol so it can run against the mock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from wholesaling.integrations.base import HealthStatus, IntegrationMode


@dataclass(frozen=True, slots=True)
class WorkflowHandleInfo:
    """The minimum we keep about a started workflow (persisted by callers, not the SDK)."""

    workflow_id: str
    run_id: str | None
    task_queue: str


@dataclass(slots=True)
class RecordedCall:
    """One recorded method call on the mock (used by tests and the synthetic pilot)."""

    method: str
    args: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TemporalGateway(Protocol):
    """Narrow Temporal surface used outside the worker process."""

    name: str
    mode: IntegrationMode

    async def connect(self) -> None:
        """Establish the connection (no-op for the mock). Safe to call more than once."""
        ...

    async def close(self) -> None:
        """Release the connection. Safe to call when never connected."""
        ...

    async def start_workflow(
        self,
        workflow: str,
        args: Sequence[Any],
        *,
        workflow_id: str,
        task_queue: str | None = None,
    ) -> WorkflowHandleInfo:
        """Start a workflow by its registered name. ``workflow_id`` is the idempotency key."""
        ...

    async def signal(self, workflow_id: str, signal: str, *args: Any) -> None:
        """Send a signal to a running workflow."""
        ...

    async def query(self, workflow_id: str, query: str, *args: Any) -> Any:
        """Run a query against a workflow and return its result."""
        ...

    async def healthcheck(self) -> HealthStatus: ...
