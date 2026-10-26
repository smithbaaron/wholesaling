"""BO-08 workflows under the Temporal time-skipping test server (marked ``temporal``; skips when
the test-server binary cannot be downloaded). Mock Sheets + mock Graph + SQLite."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.integrations.sheets.conftest import BB_V2_ID, INVESTOR_ID, NOW, seed_projection
from wholesaling.activities.graph import GraphActivityContext, set_graph_context
from wholesaling.activities.sheets import SheetsActivityContext, set_sheets_context
from wholesaling.activities.zapier import ZapierActivityContext, set_zapier_context
from wholesaling.config.settings import Settings
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.sheets import TAB_ORDER, MockSheetsGateway
from wholesaling.workers.main import build_worker
from wholesaling.workflows.buy_box_event import (
    BuyBoxEventInput,
    BuyBoxEventWorkflow,
    buy_box_event_workflow_id,
)
from wholesaling.workflows.reconciliation import (
    DailyReconciliationInput,
    DailyReconciliationWorkflow,
)
from wholesaling.workflows.sheets_sync import SheetsSyncInput, SheetsSyncWorkflow

pytestmark = pytest.mark.temporal

TASK_QUEUE = "test-bo08"
MAILBOX = "deals@example.test"
SF = async_sessionmaker[AsyncSession]


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


@pytest.fixture
def sheets() -> MockSheetsGateway:
    return MockSheetsGateway(spreadsheet_id="sheet-1")


@pytest.fixture
def graph() -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=lambda: NOW)


@pytest.fixture
def contexts(
    session_factory: SF, sheets: MockSheetsGateway, graph: MockGraphGateway
) -> AsyncIterator[Settings]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        sheets_spreadsheet_id="sheet-1",
        graph_mailbox=MAILBOX,
        zapier_parser_enabled=True,
        zapier_parser_address="abc@robot.zapier.com",
    )
    set_sheets_context(
        SheetsActivityContext(
            sheets=sheets,
            session_factory=session_factory,
            settings=settings,
            graph=graph,
            clock=lambda: NOW,
        )
    )
    set_graph_context(
        GraphActivityContext(
            gateway=graph, session_factory=session_factory, settings=settings, clock=lambda: NOW
        )
    )
    set_zapier_context(
        ZapierActivityContext(session_factory=session_factory, settings=settings, clock=lambda: NOW)
    )
    yield settings  # type: ignore[misc]
    set_sheets_context(None)
    set_graph_context(None)
    set_zapier_context(None)


async def test_sheets_sync_workflow_projects_every_tab_then_is_idempotent(
    env: WorkflowEnvironment,
    worker: Worker,
    session: AsyncSession,
    contexts: Settings,
    sheets: MockSheetsGateway,
) -> None:
    await seed_projection(session)
    handle = await env.client.start_workflow(
        SheetsSyncWorkflow.run,
        SheetsSyncInput(),
        id=f"sheets-sync-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    result = await handle.result()
    status = await handle.query(SheetsSyncWorkflow.status)
    assert result.ok and result.tabs_created == list(TAB_ORDER)
    assert result.total_changed > 0
    assert status["state"] == "done" and status["result"]["tabs_created"] == list(TAB_ORDER)

    second = await env.client.execute_workflow(
        SheetsSyncWorkflow.run,
        SheetsSyncInput(),
        id=f"sheets-sync-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    assert second.ok and second.total_changed == 0 and second.tabs_created == []
    assert set(sheets.sheets) == set(TAB_ORDER)


async def test_reconciliation_workflow_reports_mismatch_without_fixing(
    env: WorkflowEnvironment,
    worker: Worker,
    session: AsyncSession,
    contexts: Settings,
    sheets: MockSheetsGateway,
) -> None:
    await seed_projection(session)
    # No sync has run: every Postgres key is missing from Sheets; the mailbox is empty.
    result = await env.client.execute_workflow(
        DailyReconciliationWorkflow.run,
        DailyReconciliationInput(lookback_hours=48),
        id=f"reconcile-{uuid.uuid4()}",
        task_queue=TASK_QUEUE,
    )
    assert not result.ok and result.alerted
    assert {m.kind for m in result.mismatches} >= {
        "sheets_tab_missing",
        "postgres_message_missing_in_outlook",
    }
    assert sheets.sheets == {}, "reconciliation never creates or writes anything"


async def test_buy_box_event_workflow_invokes_parser_when_flag_on(
    env: WorkflowEnvironment,
    worker: Worker,
    session: AsyncSession,
    contexts: Settings,
    graph: MockGraphGateway,
) -> None:
    await seed_projection(session)
    wf_id = buy_box_event_workflow_id(str(BB_V2_ID), 2)
    result = await env.client.execute_workflow(
        BuyBoxEventWorkflow.run,
        BuyBoxEventInput(
            buy_box_id=str(BB_V2_ID),
            investor_id=str(INVESTOR_ID),
            version=2,
            event_type="buy_box.updated",
        ),
        id=wf_id,
        task_queue=TASK_QUEUE,
    )
    assert result.parser is not None and result.parser.sent
    assert result.steps == ["sheets:next_scheduled_sync", "search:not_built", "parser:sent"]
    assert result.search_profile_id is None and result.matches == []
    assert len(graph.sent) == 1
