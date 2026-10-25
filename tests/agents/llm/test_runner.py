"""AgentRunner persists one agent_runs row per call with every XC-02 field."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.agents.llm.conftest import HAIKU, SIMPLE_SCHEMA, SONNET, make_request
from wholesaling.agents.llm.interface import ProviderUnavailable
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind, prompt_hash
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.db.models.enums import AgentId
from wholesaling.db.models.ops import AgentRun


async def _rows(session: AsyncSession) -> list[AgentRun]:
    return list((await session.execute(select(AgentRun))).scalars().all())


async def test_run_persists_row_with_hash_tokens_cost_and_output(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    provider.script(
        AgentId.A4,
        TaskKind.CLASSIFY,
        responder=lambda _r: __import__(
            "wholesaling.agents.llm.mock", fromlist=["x"]
        ).ScriptedResponse(parsed={"label": "interested"}, input_tokens=1_000, output_tokens=500),
    )
    request = make_request(json_schema=SIMPLE_SCHEMA)
    response, run_id = await runner.run(
        AgentId.A4, TaskKind.CLASSIFY, request, session, correlation_id="wf-1", decision="send"
    )
    await session.commit()

    rows = await _rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == run_id
    assert row.agent == AgentId.A4
    assert row.model == HAIKU
    assert row.prompt_hash == prompt_hash(provider.requests[0])
    assert len(row.prompt_hash or "") == 64
    assert row.input_tokens == 1_000 and row.output_tokens == 500
    # 1000 * 0.10 + 500 * 0.50 per MTok = 0.00035 USD
    assert response.cost_usd == Decimal("0.000350")
    assert row.output == {"label": "interested"}
    assert row.decision == "send"
    assert row.correlation_id == "wf-1"
    assert row.duration_ms is not None and row.duration_ms >= 0
    assert row.error is None


async def test_runner_routes_by_task_kind_and_sets_model_on_request(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    await runner.run(AgentId.A3, TaskKind.DRAFT, make_request(AgentId.A3, TaskKind.DRAFT), session)
    await runner.run(AgentId.A3, TaskKind.EXTRACT, make_request(AgentId.A3), session)
    assert [r.model for r in provider.requests] == [SONNET, HAIKU]
    rows = await _rows(session)
    assert sorted(r.model for r in rows) == sorted([SONNET, HAIKU])


async def test_runner_metadata_is_authoritative_over_request_metadata(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    # Request says A4/classify, caller says A5/qa: the row and routing follow the caller.
    request = make_request(AgentId.A4, TaskKind.CLASSIFY)
    await runner.run(AgentId.A5, TaskKind.QA, request, session)
    sent = provider.requests[0]
    assert sent.metadata.agent == AgentId.A5 and sent.metadata.task_kind == TaskKind.QA
    assert sent.model == SONNET
    assert (await _rows(session))[0].agent == AgentId.A5


async def test_text_response_is_stored_under_text_key(
    runner: AgentRunner, provider: MockModelProvider, session: AsyncSession
) -> None:
    provider.script(AgentId.A3, TaskKind.DRAFT, text="Hi Jane,")
    response, _ = await runner.run(
        AgentId.A3, TaskKind.DRAFT, make_request(AgentId.A3, TaskKind.DRAFT), session
    )
    row = (await _rows(session))[0]
    assert row.output["text"] == "Hi Jane,"
    assert row.output["stop_reason"] == "end_turn"
    assert response.parsed is None


async def test_provider_failure_persists_error_row_and_reraises(
    provider: MockModelProvider, router: ModelRouter, session: AsyncSession
) -> None:
    provider.script(AgentId.A1, TaskKind.EXTRACT, raise_error=ProviderUnavailable("api down"))
    runner = AgentRunner(provider, router, commit_on_error=False)
    with pytest.raises(ProviderUnavailable):
        await runner.run(
            AgentId.A1,
            TaskKind.EXTRACT,
            make_request(AgentId.A1, TaskKind.EXTRACT),
            session,
            correlation_id="wf-err",
        )
    rows = await _rows(session)
    assert len(rows) == 1
    row = rows[0]
    assert row.decision == "error"
    assert row.error == "ProviderUnavailable: api down"
    assert row.model == HAIKU
    assert row.prompt_hash and row.input_tokens == 0 and row.cost_usd == Decimal("0")
    assert row.correlation_id == "wf-err"
    assert row.output == {}


async def test_error_row_is_committed_by_default_so_rollback_keeps_it(
    provider: MockModelProvider, router: ModelRouter, session: AsyncSession
) -> None:
    provider.script(AgentId.A1, TaskKind.EXTRACT, raise_error=RuntimeError("x"))
    runner = AgentRunner(provider, router)
    with pytest.raises(RuntimeError):
        await runner.run(AgentId.A1, TaskKind.EXTRACT, make_request(AgentId.A1), session)
    await session.rollback()
    assert len(await _rows(session)) == 1


async def test_unknown_model_logs_zero_cost_and_notes_it(
    provider: MockModelProvider,
    settings,
    session: AsyncSession,  # type: ignore[no-untyped-def]
) -> None:
    router = ModelRouter.from_settings(settings, {"A2": {"*": "grok-4"}})
    runner = AgentRunner(provider, router)
    response, _ = await runner.run(AgentId.A2, TaskKind.EXTRACT, make_request(AgentId.A2), session)
    row = (await _rows(session))[0]
    assert response.cost_usd is None
    assert row.cost_usd == Decimal("0")
    assert row.error == "unpriced model grok-4"
    assert row.decision is None
