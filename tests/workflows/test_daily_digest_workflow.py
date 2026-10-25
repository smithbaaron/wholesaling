"""DailyDigestWorkflow under the Temporal time-skipping test server (marked ``temporal``;
skips when the test-server binary cannot be downloaded)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.reporting.conftest import NOW, TZ, seed_kpis
from wholesaling.activities.digest import DigestActivityContext, set_digest_context
from wholesaling.config.settings import Settings
from wholesaling.reporting.digest import LogSink
from wholesaling.workers.main import build_worker
from wholesaling.workflows.daily_digest import DailyDigestInput, DailyDigestWorkflow

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-daily-digest"


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
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


async def test_digest_workflow_runs_activity_and_reports(
    env: WorkflowEnvironment,
    worker: Worker,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
) -> None:
    await seed_kpis(session)
    sink = LogSink()
    settings = Settings(_env_file=None, app_env="test", operator_timezone=TZ)  # type: ignore[call-arg]
    set_digest_context(
        DigestActivityContext(
            session_factory=session_factory, settings=settings, sinks=[sink], clock=lambda: NOW
        )
    )
    try:
        handle = await env.client.start_workflow(
            DailyDigestWorkflow.run,
            DailyDigestInput(),
            id=f"daily-digest-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        result = await handle.result()
        status = await handle.query(DailyDigestWorkflow.status)
    finally:
        set_digest_context(None)
    assert result.day == "2026-10-20"
    assert result.queue_depth == 3
    assert result.sinks == ["log"] and result.failed_sinks == []
    assert status["state"] == "done"
    assert status["result"]["day"] == "2026-10-20"
    assert len(sink.sent) == 1
