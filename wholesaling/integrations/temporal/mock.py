"""In-memory Temporal gateway. Records every call and executes nothing.

Selected when ``TEMPORAL_MODE=mock`` (the default). Tests assert on ``calls``; the synthetic
pilot uses it so the API can be exercised with no Temporal server.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.temporal.interface import RecordedCall, WorkflowHandleInfo


class MockTemporalGateway:
    name = "temporal"
    mode: IntegrationMode = "mock"

    def __init__(self, task_queue: str = "wholesaling-main") -> None:
        self.task_queue = task_queue
        self.calls: list[RecordedCall] = []
        self.workflows: dict[str, WorkflowHandleInfo] = {}
        self.signals: dict[str, list[tuple[str, tuple[Any, ...]]]] = {}
        self.query_responses: dict[str, Any] = {}
        self.connected = False
        self.healthy = True

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append(RecordedCall(method=method, args=args))

    async def connect(self) -> None:
        self._record("connect")
        self.connected = True

    async def close(self) -> None:
        self._record("close")
        self.connected = False

    async def start_workflow(
        self,
        workflow: str,
        args: Sequence[Any],
        *,
        workflow_id: str,
        task_queue: str | None = None,
    ) -> WorkflowHandleInfo:
        queue = task_queue or self.task_queue
        self._record(
            "start_workflow",
            workflow=workflow,
            args=list(args),
            workflow_id=workflow_id,
            task_queue=queue,
        )
        if workflow_id in self.workflows:
            # Mirror Temporal's default id policy: a running workflow with the same id is reused.
            return self.workflows[workflow_id]
        info = WorkflowHandleInfo(
            workflow_id=workflow_id, run_id=uuid.uuid4().hex, task_queue=queue
        )
        self.workflows[workflow_id] = info
        return info

    async def signal(self, workflow_id: str, signal: str, *args: Any) -> None:
        self._record("signal", workflow_id=workflow_id, signal=signal, args=list(args))
        self.signals.setdefault(workflow_id, []).append((signal, args))

    async def query(self, workflow_id: str, query: str, *args: Any) -> Any:
        self._record("query", workflow_id=workflow_id, query=query, args=list(args))
        return self.query_responses.get(f"{workflow_id}:{query}", self.query_responses.get(query))

    async def healthcheck(self) -> HealthStatus:
        self._record("healthcheck")
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=self.healthy,
            detail="mock gateway" if self.healthy else "mock gateway forced unhealthy",
        )

    def reset(self) -> None:
        self.calls.clear()
        self.workflows.clear()
        self.signals.clear()
        self.query_responses.clear()
