"""``AgentRunner``: route, call, price, and persist every model invocation (REQUIREMENTS.md XC-02).

One ``agent_runs`` row per call, success or failure, with: agent, model, prompt_hash, tokens,
cost_usd, structured output, decision, duration_ms, correlation_id, error. Agents never call a
provider directly; they go through ``BaseAgent`` which goes through here.

Transactions: the run row is added to the caller's session and flushed, like every repository in
``wholesaling.db.repositories``; the caller commits. On provider failure the row is committed
immediately (``commit_on_error=True`` by default) so a failed run is logged even when the caller
rolls back the surrounding work.
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.agents.llm.interface import ModelProvider
from wholesaling.agents.llm.models import (
    ModelRequest,
    ModelResponse,
    TaskKind,
    prompt_hash,
)
from wholesaling.agents.llm.pricing import estimate_cost
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.db.models.enums import AgentId
from wholesaling.db.models.ops import AgentRun
from wholesaling.logging import get_logger

log = get_logger(__name__)

ERROR_DECISION = "error"
_MAX_ERROR_LEN = 2000

CostEstimator = Callable[[str, int, int], Decimal | None]


class AgentRunner:
    def __init__(
        self,
        provider: ModelProvider,
        router: ModelRouter,
        *,
        cost_estimator: CostEstimator = estimate_cost,
        commit_on_error: bool = True,
    ) -> None:
        self.provider = provider
        self.router = router
        self._estimate = cost_estimator
        self._commit_on_error = commit_on_error

    async def run(
        self,
        agent: AgentId,
        task_kind: TaskKind,
        request: ModelRequest,
        session: AsyncSession,
        *,
        correlation_id: str | None = None,
        decision: str | None = None,
    ) -> tuple[ModelResponse, uuid.UUID]:
        """Route, call the provider, persist the run. Returns ``(response, agent_run_id)``.

        ``agent`` / ``task_kind`` are authoritative: the request's metadata is rewritten to match
        so the hash, the routing and the row never disagree. Provider errors are re-raised after
        the error row is persisted.
        """
        metadata = dataclasses.replace(request.metadata, agent=agent, task_kind=task_kind)
        model = self.router.route(agent, task_kind)
        routed = dataclasses.replace(request, metadata=metadata, model=model)
        digest = prompt_hash(routed)

        started = time.perf_counter()
        try:
            response = await self.provider.complete(routed)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            run = AgentRun(
                agent=agent,
                model=model,
                prompt_hash=digest,
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("0"),
                output={},
                decision=ERROR_DECISION,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                error=_format_error(exc),
            )
            session.add(run)
            await session.flush()
            if self._commit_on_error:
                await session.commit()
            log.warning(
                "llm.run.failed",
                agent=agent.value,
                task_kind=task_kind.value,
                model=model,
                prompt_hash=digest,
                duration_ms=duration_ms,
                error=type(exc).__name__,
                correlation_id=correlation_id,
                agent_run_id=str(run.id),
            )
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        if response.latency_ms == 0:
            response.latency_ms = duration_ms
        cost = self._estimate(response.model, response.input_tokens, response.output_tokens)
        response.cost_usd = cost

        run = AgentRun(
            agent=agent,
            model=response.model,
            prompt_hash=digest,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=cost if cost is not None else Decimal("0"),
            output=_output_document(response),
            decision=decision,
            duration_ms=duration_ms,
            correlation_id=correlation_id,
            error=None if cost is not None else f"unpriced model {response.model}",
        )
        session.add(run)
        await session.flush()
        log.info(
            "llm.run.completed",
            agent=agent.value,
            task_kind=task_kind.value,
            model=response.model,
            prompt_hash=digest,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=str(cost) if cost is not None else None,
            duration_ms=duration_ms,
            decision=decision,
            correlation_id=correlation_id,
            agent_run_id=str(run.id),
        )
        return response, run.id


def _output_document(response: ModelResponse) -> dict[str, Any]:
    """What goes in ``agent_runs.output``: the structured object when there is one."""
    if response.parsed is not None:
        return response.parsed
    doc: dict[str, Any] = {"text": response.text}
    if response.tool_calls:
        doc["tool_calls"] = [
            {"id": c.id, "name": c.name, "input": c.input} for c in response.tool_calls
        ]
    if response.stop_reason:
        doc["stop_reason"] = response.stop_reason
    return doc


def _format_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:_MAX_ERROR_LEN]
