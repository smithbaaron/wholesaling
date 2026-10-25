"""HelloWorkflow under the Temporal time-skipping test server.

Marked ``temporal``: the first run downloads the test-server binary. If that download is
blocked (sandbox / offline), the module skips with the underlying error instead of failing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from wholesaling.workers.main import build_worker
from wholesaling.workflows.hello import HelloWorkflow

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-hello"


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    # CI or an offline machine can pre-download the test server and point at it.
    existing = os.environ.get("TEMPORAL_TEST_SERVER_PATH")
    try:
        environment = await WorkflowEnvironment.start_time_skipping(
            test_server_existing_path=existing
        )
    except Exception as exc:  # binary download / spawn failure
        pytest.skip(f"Temporal time-skipping test server unavailable: {exc!r}")
    async with environment:
        yield environment


@pytest.fixture
async def worker(env: WorkflowEnvironment) -> AsyncIterator[Worker]:
    w = build_worker(env.client, TASK_QUEUE)
    async with w:
        yield w


async def test_hello_completes_after_durable_sleep(
    env: WorkflowEnvironment, worker: Worker
) -> None:
    handle = await env.client.start_workflow(
        HelloWorkflow.run,
        args=["Aaron", 3600],
        id=f"hello-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    status = await handle.query(HelloWorkflow.status)
    assert status["state"] in ("waiting", "greeting", "done")
    assert status["name"] == "Aaron"
    # Time skipping: the one-hour sleep elapses immediately when we await the result.
    assert await handle.result() == "Hello, Aaron!"
    done = await handle.query(HelloWorkflow.status)
    assert done["state"] == "done"
    assert done["greeting"] == "Hello, Aaron!"
    assert done["remaining_seconds"] == 0


async def test_pause_and_resume_signals(env: WorkflowEnvironment, worker: Worker) -> None:
    handle = await env.client.start_workflow(
        HelloWorkflow.run,
        args=["Paused", 600],
        id=f"hello-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    await handle.signal(HelloWorkflow.pause)
    status = await handle.query(HelloWorkflow.status)
    assert status["paused"] is True
    assert status["state"] == "paused"

    # Advance well past the original delay: a paused workflow must not finish.
    with env.auto_time_skipping_disabled():
        await env.sleep(3600)
    status = await handle.query(HelloWorkflow.status)
    assert status["state"] == "paused"
    assert status["greeting"] is None

    await handle.signal(HelloWorkflow.resume)
    assert await handle.result() == "Hello, Paused!"
    final = await handle.query(HelloWorkflow.status)
    assert final["paused"] is False
    assert final["state"] == "done"


async def test_zero_delay_runs_immediately(env: WorkflowEnvironment, worker: Worker) -> None:
    result = await env.client.execute_workflow(
        HelloWorkflow.run,
        args=["Now", 0],
        id=f"hello-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    assert result == "Hello, Now!"


async def test_workflow_type_name_is_stable(env: WorkflowEnvironment, worker: Worker) -> None:
    # Later sequence workflows are started by name from the API; the name is part of the contract.
    handle = await env.client.start_workflow(
        "HelloWorkflow",
        args=["ByName", 0],
        id=f"hello-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    try:
        assert await handle.result() == "Hello, ByName!"
    except WorkflowFailureError as exc:  # pragma: no cover - diagnostic only
        pytest.fail(f"workflow failed: {exc.cause!r}")
