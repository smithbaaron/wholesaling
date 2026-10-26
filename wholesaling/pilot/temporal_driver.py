"""``--temporal``: the same pilot through the Temporal time-skipping test environment.

Discovery, model scripting, grading and the report are inherited from ``PilotRunner``; only the
orchestration differs:

- a real ``Worker`` (``workers.main.build_worker``) runs the real workflows and activities;
- ``LiveTemporalGateway`` over the test environment's client is the gateway the webhook, the
  review route and the conversation activities use, so the ``reply`` / ``opt_out`` / ``bounce``
  signals and the ``human_decision`` signal travel through Temporal for real (a payload the
  handler could not take would fail the workflow task here, where the simulator records a
  contract error);
- time moves with ``env.sleep`` instead of the simulator: the driver polls the database for
  newly sent touches, injects the scripted replies when they fall due, approves queued drafts
  through the review route after the operator delay, and otherwise skips time forward;
- the WF-02 starter (``activities/sequence_starter.start_investor_sequence``) starts each
  persona's sequence through the live gateway, and the review route resolves inbound workflows
  from ``meta.temporal_workflow_id`` the way production does.

The test-server binary is downloaded on first use; where that is blocked the driver raises
``RuntimeError`` and the CLI exits 2. **This driver has not been executed in the build sandbox**
(the download is blocked there); see docs/pilot.md.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from wholesaling.activities import conversation as conv_acts
from wholesaling.activities import discovery as discovery_acts
from wholesaling.activities import graph as graph_acts
from wholesaling.activities import sequence_starter as starter_acts
from wholesaling.agents.a4 import MODEL_OVERRIDES
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.db.models import Message
from wholesaling.db.models.enums import Direction
from wholesaling.pilot.report import PilotReport
from wholesaling.pilot.runner import (
    DEFAULT_OPERATOR_DELAY,
    DEFAULT_SIM_START,
    Persona,
    PilotRunner,
)
from wholesaling.pilot.scenarios import PILOT_MARKETS, ScriptedReply, scenarios_for_markets
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

TASK_QUEUE = "pilot-temporal"
POLL_INTERVAL_S = 0.25
TIME_STEP = timedelta(hours=1)
MAX_STEPS = 24 * 45  # 45 simulated days of hourly steps
STALL_STEPS = 24 * 3  # three simulated days without any change: stop and report


class TemporalPilotRunner(PilotRunner):
    """Run the pilot on a real (time-skipping) Temporal server."""

    def __init__(
        self,
        *,
        markets: Sequence[str] = PILOT_MARKETS,
        database_url: str | None = None,
        sim_start: datetime = DEFAULT_SIM_START,
        operator_delay: timedelta = DEFAULT_OPERATOR_DELAY,
        log_level: str = "WARNING",
    ) -> None:
        super().__init__(
            markets=markets,
            database_url=database_url,
            sim_start=sim_start,
            operator_delay=operator_delay,
            log_level=log_level,
        )
        # The worker polls TASK_QUEUE; the webhook route starts inbound workflows on the
        # settings' default queue, so the settings must name it before the app is built.
        self.settings = self.settings.model_copy(update={"temporal_task_queue": TASK_QUEUE})
        self._env: Any = None
        self._skipped = timedelta(0)
        self._stalled = False
        self._wall_anchor = datetime.now(tz=UTC)
        self._pending_replies: list[tuple[datetime, Persona, int, ScriptedReply]] = []
        self._pending_approvals: list[tuple[datetime, str, str]] = []
        self._seen_touches: set[tuple[str, int]] = set()

    # The pilot clock: real time shifted by everything ``env.sleep`` skipped, pinned so the
    # first send lands inside the business-hour window like the simulated run.
    def _clock(self) -> datetime:
        return self.sim_start + (datetime.now(tz=UTC) - self._wall_anchor) + self._skipped

    async def run(
        self,
        policy_level_overrides: dict[PolicyAction, AutonomyLevel] | None = None,
        approve_everything: bool = True,
        seed: int = 20261027,
    ) -> PilotReport:
        try:
            from temporalio.testing import WorkflowEnvironment
        except ImportError as exc:  # pragma: no cover - temporalio is a hard dependency
            msg = f"Temporal SDK unavailable: {exc}"
            raise RuntimeError(msg) from exc
        try:
            env = await WorkflowEnvironment.start_time_skipping()
        except Exception as exc:
            msg = f"Temporal time-skipping test server unavailable: {exc!r}"
            raise RuntimeError(msg) from exc
        self._env = env
        try:
            async with env:
                return await self._run_with_env(
                    env,
                    policy_level_overrides=policy_level_overrides,
                    approve_everything=approve_everything,
                    seed=seed,
                )
        finally:
            self._env = None

    async def _run_with_env(
        self,
        env: Any,
        *,
        policy_level_overrides: dict[PolicyAction, AutonomyLevel] | None,
        approve_everything: bool,
        seed: int,
    ) -> PilotReport:
        from wholesaling.integrations.temporal.client import LiveTemporalGateway
        from wholesaling.workers.main import build_worker

        started = datetime.now(tz=UTC)
        t0 = time.perf_counter()
        self._approve_everything = approve_everything
        overrides = dict(policy_level_overrides or {})
        self._level = overrides.get(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L0)
        scenarios = scenarios_for_markets(self.markets)
        self.scripts.install(scenarios)
        await self._prepare_database(overrides)
        assert self.session_factory is not None

        live = LiveTemporalGateway(self.settings, client=env.client)
        self.gateway = live  # type: ignore[assignment]  # the API and activities signal through it
        self.graph.set_clock(self._clock)
        graph_acts.set_graph_context(
            graph_acts.GraphActivityContext(
                gateway=self.graph,
                session_factory=self.session_factory,
                settings=self.settings,
                clock=self._clock,
            )
        )
        conv_acts.set_conversation_context(
            conv_acts.ConversationContext(
                session_factory=self.session_factory,
                runner=AgentRunner(
                    self.provider, ModelRouter.from_settings(self.settings, MODEL_OVERRIDES)
                ),
                temporal=live,
                settings=self.settings,
                clock=self._clock,
                prompt_values={"company": self.settings.sender_company},
            )
        )
        discovery_acts.configure(self.session_factory, self.providers)
        starter_acts.set_starter_context(self._starter_context())
        try:
            await self._discover(scenarios)
            worker = build_worker(env.client, TASK_QUEUE)
            async with worker, self._api() as client:
                self._client = client
                await self._start_all_sequences()
                await self._drive(env)
                self.delta_replay = await self._replay_delta_poll()
                report = await self._collect(
                    scenarios, started=started, wall=time.perf_counter() - t0, seed=seed
                )
            report.mode = "temporal"
            if self._stalled:
                report.notes.append(
                    "Temporal mode: the drive loop stalled (no database change for "
                    f"{STALL_STEPS} hourly steps); sequences still open are reported as they were. "
                    "A sequence that never closes after a reply is GAP-01 (signal payload "
                    "TypeError fails the workflow task)."
                )
            await self._record_report(report)
            return report
        finally:
            self._client = None
            conv_acts.set_conversation_context(None)
            graph_acts.set_graph_context(None)
            starter_acts.set_starter_context(None)
            discovery_acts.reset()
            if self.engine is not None:
                await self.engine.dispose()

    # -- sequences -----------------------------------------------------------------------------

    async def _start_all_sequences(self) -> None:
        """The production starter's candidates (priority tier first), one at a time so each
        persona's policy scope applies to its own workflow start."""
        for index, (persona, candidate) in enumerate(await self._starter_candidates()):
            await self._start_one(persona, candidate, index)

    def _starter_context(self) -> starter_acts.StarterContext:
        assert self.session_factory is not None
        return starter_acts.StarterContext(
            session_factory=self.session_factory,
            temporal=self.gateway,
            settings=self.settings,
            clock=self._clock,
        )

    async def _start_one(
        self, persona: Persona, candidate: starter_acts.StartCandidate, order: int
    ) -> None:
        scope, saved = await self._scoped_levels(persona)
        if scope:
            await self._set_levels(scope, f"pilot scope for {persona.scenario.id}")
        started = await starter_acts.start_investor_sequence(self._starter_context(), candidate)
        persona.sequence_id = uuid.UUID(started.sequence_id)
        persona.workflow_id = started.workflow_id
        persona.start_order = order
        self._sequence_persona[started.sequence_id] = persona
        await self._wait_parked(persona.workflow_id)
        if scope:
            await self._set_levels(saved, f"pilot scope restored after {persona.scenario.id}")

    async def _wait_parked(self, workflow_id: str, *, max_wait: float = 30.0) -> None:
        """Poll ``status`` until the workflow is waiting (human, sleep, window) or closed."""
        deadline = time.monotonic() + max_wait
        parked = {
            "awaiting_human",
            "sleeping",
            "paused",
            "waiting_send_window",
            "done",
            "cancelled",
        }
        while time.monotonic() < deadline:
            try:
                status = await self.gateway.query(workflow_id, "status")
            except Exception:
                return
            state = status.get("state") if isinstance(status, dict) else None
            if state in parked:
                return
            await asyncio.sleep(POLL_INTERVAL_S)

    # -- the drive loop -------------------------------------------------------------------------

    async def _drive(self, env: Any) -> None:
        last_fingerprint: tuple[int, ...] | None = None
        quiet_steps = 0
        for _ in range(MAX_STEPS):
            await asyncio.sleep(POLL_INTERVAL_S)
            await self._observe_touches()
            now = self._clock()
            acted = await self._inject_due(now)
            acted = await self._approve_due(now) or acted
            acted = await self._schedule_approvals(now) or acted
            if acted:
                quiet_steps = 0
                continue
            if (
                await self._all_sequences_closed()
                and not self._pending_replies
                and (not self._pending_approvals)
            ):
                return
            fingerprint = (
                len(self._seen_touches),
                sum(len(p.injected) for p in self.personas.values()),
                self._operator.approvals,
                len(self._pending_replies),
                len(self._pending_approvals),
            )
            quiet_steps = quiet_steps + 1 if fingerprint == last_fingerprint else 0
            last_fingerprint = fingerprint
            if quiet_steps >= STALL_STEPS and not self._pending_replies:
                self._stalled = True
                return
            step = self._next_step(now)
            self._skipped += step
            await env.sleep(step)
        self._stalled = True

    def _next_step(self, now: datetime) -> timedelta:
        due = [at for at, *_ in self._pending_replies] + [at for at, *_ in self._pending_approvals]
        if due:
            return max(min(due) - now, timedelta(seconds=1))
        return TIME_STEP

    async def _observe_touches(self) -> None:
        assert self.session_factory is not None
        async with self.session_factory() as s:
            stmt = select(Message).where(
                Message.direction == Direction.OUT,
                Message.sequence_id.is_not(None),
                Message.sent_at.is_not(None),
            )
            for m in (await s.execute(stmt)).scalars().all():
                key = (str(m.sequence_id), int(m.sequence_step or 0))
                if key in self._seen_touches:
                    continue
                self._seen_touches.add(key)
                persona = self._sequence_persona.get(key[0])
                if persona is None or m.sent_at is None:
                    continue
                sent_at = m.sent_at if m.sent_at.tzinfo else m.sent_at.replace(tzinfo=UTC)
                persona.touches[key[1]] = sent_at
                if not (m.meta or {}).get("human_approved"):
                    persona.auto_touches.add(key[1])
                for index, reply in enumerate(persona.scenario.replies):
                    if reply.after_touch == key[1] + 1 and index not in persona.scheduled_replies:
                        persona.scheduled_replies.add(index)
                        at = sent_at + timedelta(hours=reply.delay_hours)
                        self._pending_replies.append((at, persona, index, reply))

    async def _inject_due(self, now: datetime) -> bool:
        due = [p for p in self._pending_replies if p[0] <= now]
        if not due:
            return False
        self._pending_replies = [p for p in self._pending_replies if p[0] > now]
        for _at, persona, index, reply in sorted(due, key=lambda p: (p[0], p[1].scenario.id)):
            await self._inject_reply_temporal(persona, index, reply)
        return True

    async def _inject_reply_temporal(
        self, persona: Persona, index: int, reply: ScriptedReply
    ) -> None:
        # The simulator's ``settle`` has no meaning here; inject through the shared code, then
        # wait for the inbound workflow to park.
        scope, saved = await self._scoped_levels(persona)
        if scope:
            await self._set_levels(scope, f"pilot scope for {persona.scenario.id}")
        self.sim.clock = self._clock()  # only read for the injected message's timestamp
        await PilotRunner._inject_reply(self, persona, index, reply)
        injected = persona.injected[-1]
        await self._wait_parked(injected.workflow_id)
        if scope:
            await self._set_levels(saved, f"pilot scope restored after {persona.scenario.id}")

    async def _schedule_approvals(self, now: datetime) -> bool:
        if not self._approve_everything or self._client is None:
            return False
        response = await self._client.get("/api/review", headers={"accept": "application/json"})
        response.raise_for_status()
        scheduled = False
        for item in response.json()["items"]:
            mid = item["message_id"]
            if mid in self._operator.scheduled:
                continue
            self._operator.scheduled.add(mid)
            self._pending_approvals.append((now + self.operator_delay, mid, item["action"]))
            scheduled = True
        return scheduled

    async def _approve_due(self, now: datetime) -> bool:
        due = [p for p in self._pending_approvals if p[0] <= now]
        if not due:
            return False
        self._pending_approvals = [p for p in self._pending_approvals if p[0] > now]
        for _at, mid, action in due:
            result = await self._post(f"/review/{mid}/approve")
            self._operator.approvals += 1
            if result.get("edited"):
                self._operator.approvals_with_edits += 1
            self._operator.log.append(
                {"message_id": mid, "action": action, "at": now.isoformat(), **result}
            )
        await asyncio.sleep(POLL_INTERVAL_S)
        return True

    async def _all_sequences_closed(self) -> bool:
        for persona in self.personas.values():
            if persona.workflow_id is None:
                continue
            try:
                status = await self.gateway.query(persona.workflow_id, "status")
            except Exception:  # noqa: S112 - a closed workflow may reject the query: it is done
                continue
            if isinstance(status, dict) and status.get("state") not in ("done", "cancelled"):
                return False
        return True


__all__ = [
    "MAX_STEPS",
    "POLL_INTERVAL_S",
    "STALL_STEPS",
    "TASK_QUEUE",
    "TIME_STEP",
    "TemporalPilotRunner",
]
