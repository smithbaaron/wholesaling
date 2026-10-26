"""``PilotRunner``: the whole investor loop on SQLite in mock mode, without a Temporal server.

    fresh database -> policies seeded at L0 -> M1 -> A1 + A2 for the pilot markets
      -> the WF-02 starter (``activities/sequence_starter.py``) lists each market's eligible
         investors, priority tier first, and starts one InvestorSequenceWorkflow per persona
         with a primary email (suppressed addresses and investors without an email are skipped)
      -> scripted replies injected into the mock Graph gateway at the scripted moment and
         announced through POST /webhooks/graph (twice: the duplicate must be a no-op)
      -> GraphInboundMessageWorkflow: A4 classify -> buy box -> A4 draft -> A5 QA -> policy
      -> the operator approves every queued draft through POST /review/{id}/approve (the same
         route, auth and signal path the HTMX page uses) one simulated hour later
      -> sequences and replies send through the mock gateway in the original conversation
      -> delta poll replay (INV-24) -> collect -> grade -> PilotReport

Everything the production worker would run is the production code: activities, repositories,
policy engine, review route, webhook route, workflow ``run()`` loops, the sequence starter and
the A5 review of every sequence draft. The one stand-in is the simulator that drives the workflow
loops (``sim.py``) in place of a Temporal server; the pilot schedules the starter's candidates
two simulated minutes apart (one workflow at a time) instead of letting the kickoff activity
start them in one go, so a run stays deterministic.
"""

from __future__ import annotations

import os
import random
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, BasicAuth
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from wholesaling.activities import conversation as conv_acts
from wholesaling.activities import discovery as discovery_acts
from wholesaling.activities import graph as graph_acts
from wholesaling.activities import sequence_starter as starter_acts
from wholesaling.agents.a4 import MODEL_OVERRIDES
from wholesaling.agents.llm.mock import MockModelProvider, ScriptedResponse
from wholesaling.agents.llm.models import ModelRequest, TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.api.auth import hash_password
from wholesaling.api.main import create_app
from wholesaling.api.routes.review import EVENT_APPROVED, EVENT_APPROVED_WITH_EDITS, EVENT_REJECTED
from wholesaling.config.settings import Settings
from wholesaling.db import Base, make_engine, make_session_factory
from wholesaling.db.models import (
    AgentRun,
    AuditEvent,
    BuyBox,
    Contact,
    Investor,
    Market,
    Message,
    QaReview,
    Suppression,
    Thread,
)
from wholesaling.db.models.enums import (
    Actor,
    AgentId,
    Channel,
    ContactChannel,
    Direction,
    MarketTier,
    PolicyDecision,
)
from wholesaling.db.models.messaging import Sequence as SequenceRow
from wholesaling.db.normalize import normalize_email
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.logging import configure_logging
from wholesaling.pilot.report import Aggregates, Check, Gap, PilotReport, ScenarioResult, Status
from wholesaling.pilot.scenarios import (
    A3_TEMPLATE_QA,
    PILOT_MARKETS,
    PRIORITY_SCORE,
    Expected,
    Scenario,
    ScriptedReply,
    scenarios_for_markets,
)
from wholesaling.pilot.sim import (
    ActivityCall,
    SimTemporalGateway,
    WorkflowSimulator,
    patched_workflow_modules,
)
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.reporting.kpis import all_promotion_readiness
from wholesaling.workflows.graph_maintenance import inbound_workflow_id
from wholesaling.workflows.investor_sequence import (
    WORKFLOW_NAME as SEQUENCE_WORKFLOW,
)

PILOT_MAILBOX = "deals@pilot.example"
PILOT_SUBSCRIPTION = "pilot-subscription"
PILOT_CLIENT_STATE = "pilot-client-state"
OPERATOR = "pilot-operator"
OPERATOR_PASSWORD = "pilot-only-not-a-real-secret"  # noqa: S105 - synthetic pilot credential
# Tuesday 2026-10-27 10:00 America/Chicago (11:00 America/New_York): inside both send windows.
DEFAULT_SIM_START = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
AS_OF = datetime(2026, 10, 25, tzinfo=UTC)  # discovery anchor inside the fixtures' window
DEFAULT_OPERATOR_DELAY = timedelta(hours=1)
SEQUENCE_START_SPACING = timedelta(minutes=2)
INVESTOR_ACTIONS = (
    PolicyAction.INVESTOR_INITIAL_OUTREACH,
    PolicyAction.INVESTOR_FOLLOWUP,
    PolicyAction.INVESTOR_REPLY,
    PolicyAction.OPT_OUT_SUPPRESSION,
)


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes for ``DateTime(timezone=True)``; treat them as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------------------------
# Runtime binding of a scenario to a discovered investor
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class InjectedMessage:
    reply_index: int
    graph_id: str
    internet_message_id: str
    conversation_id: str | None
    at: datetime
    workflow_id: str


@dataclass(slots=True)
class Persona:
    scenario: Scenario
    investor_id: uuid.UUID
    investor_name: str
    market: str
    score: int | None
    email: str | None
    sequence_id: uuid.UUID | None = None
    workflow_id: str | None = None
    start_order: int | None = None
    starter_skip_reason: str | None = None  # why the WF-02 starter did not start a sequence
    unreachable_recorded: bool = False  # run_a2 wrote ``investor.unreachable`` (GAP-07)
    touches: dict[int, datetime] = field(default_factory=dict)  # step -> sent at
    auto_touches: set[int] = field(default_factory=set)
    scheduled_replies: set[int] = field(default_factory=set)
    injected: list[InjectedMessage] = field(default_factory=list)

    @property
    def tier(self) -> str:
        return "priority" if (self.score or 0) >= PRIORITY_SCORE else "prospect"


@dataclass(slots=True)
class _Operator:
    approvals: int = 0
    approvals_with_edits: int = 0
    rejections: int = 0
    scheduled: set[str] = field(default_factory=set)
    log: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Model scripting
# --------------------------------------------------------------------------------------------

_LATEST_MARKER = "LATEST INBOUND MESSAGE"


def _latest_section(request: ModelRequest) -> str:
    content = "\n".join(m.content for m in request.messages)
    return content.rsplit(_LATEST_MARKER, 1)[-1]


def _draft_section(request: ModelRequest) -> str:
    content = "\n".join(m.content for m in request.messages)
    return content.rsplit("DRAFT BODY:", 1)[-1]


class ScriptBook:
    """Installs each scripted reply's model outputs on the mock provider and records every
    model call that no script covered (the mock then returns schema placeholders)."""

    def __init__(self, provider: MockModelProvider) -> None:
        self.provider = provider
        self.unscripted: list[str] = []
        self.hits: Counter[str] = Counter()

    def install(self, scenarios: Iterable[Scenario]) -> None:
        for scenario in scenarios:
            for index, reply in enumerate(scenario.replies):
                self._install_reply(scenario, index, reply)
        # A5 on the locked A3 templates (every sequence draft is QA'd before the policy).
        for needle, verdict in A3_TEMPLATE_QA.items():
            self.provider.script_matching(
                self._a5_predicate(needle), self._responder("a3_template", TaskKind.QA, verdict)
            )
        self.provider.script_matching(lambda _r: True, self._unscripted)

    def _install_reply(self, scenario: Scenario, index: int, reply: ScriptedReply) -> None:
        tag = f"{scenario.id}#{index}"
        script = reply.model
        for kind, parsed in (
            (TaskKind.CLASSIFY, script.classify),
            (TaskKind.EXTRACT, script.extract),
            (TaskKind.DRAFT, script.draft),
        ):
            if parsed is None:
                continue
            self.provider.script_matching(
                self._a4_predicate(kind, reply.match), self._responder(tag, kind, parsed)
            )
        if script.qa is not None and script.draft is not None:
            self.provider.script_matching(
                self._a5_predicate(str(script.draft["body"])),
                self._responder(tag, TaskKind.QA, script.qa),
            )

    @staticmethod
    def _a5_predicate(draft_body: str) -> Callable[[ModelRequest], bool]:
        def _pred(r: ModelRequest) -> bool:
            return (
                r.metadata.agent is AgentId.A5
                and r.metadata.task_kind is TaskKind.QA
                and draft_body in _draft_section(r)
            )

        return _pred

    @staticmethod
    def _a4_predicate(kind: TaskKind, match: str) -> Callable[[ModelRequest], bool]:
        def _pred(r: ModelRequest) -> bool:
            return (
                r.metadata.agent is AgentId.A4
                and r.metadata.task_kind is kind
                and match in _latest_section(r)
            )

        return _pred

    def _responder(
        self, tag: str, kind: TaskKind, parsed: dict[str, Any]
    ) -> Callable[[ModelRequest], ScriptedResponse]:
        def _respond(_r: ModelRequest) -> ScriptedResponse:
            self.hits[f"{tag}:{kind.value}"] += 1
            return ScriptedResponse(parsed=dict(parsed))

        return _respond

    def _unscripted(self, request: ModelRequest) -> ScriptedResponse:
        kind = request.metadata.task_kind
        agent = request.metadata.agent.value
        excerpt = _latest_section(request).strip().replace("\n", " ")[:80]
        self.unscripted.append(f"{agent}:{kind.value} <- {excerpt!r}")
        if kind is TaskKind.CLASSIFY:
            return ScriptedResponse(
                parsed={
                    "classification": "ambiguous",
                    "confidence": 0.5,
                    "rationale": "unscripted pilot message",
                    "is_auto_reply": False,
                    "reconnect_date": None,
                }
            )
        if kind is TaskKind.EXTRACT:
            return ScriptedResponse(parsed={"buy_boxes": [], "rationale": "unscripted"})
        if kind is TaskKind.DRAFT:
            return ScriptedResponse(
                parsed={
                    "subject": "",
                    "body": "Thanks for your note. Could you tell us what you buy?",
                    "asks_for": [],
                    "contains_sensitive": False,
                }
            )
        return ScriptedResponse(
            parsed={
                "verdict": "approve",
                "confidence": 0.5,
                "flags": [],
                "rewrite_body": None,
                "rationale": "unscripted pilot review",
            }
        )


# --------------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------------


def pilot_settings(database_url: str, *, log_level: str = "WARNING") -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        log_level=log_level,
        database_url=database_url,
        graph_mode="mock",
        temporal_mode="mock",
        model_provider="mock",
        data_provider_mode="mock",
        graph_mailbox=PILOT_MAILBOX,
        graph_webhook_url="https://pilot.example/webhooks/graph",
        graph_webhook_client_state=PILOT_CLIENT_STATE,
        sender_postal_address="1 Pilot Way, Houston, TX 77002",
        operator_username=OPERATOR,
        operator_password_hash=hash_password(
            OPERATOR_PASSWORD, iterations=1_000, salt=b"pilot-salt-0123"
        ),
    )


class PilotRunner:
    """Run the synthetic pilot once. ``run()`` returns a ``PilotReport``."""

    def __init__(
        self,
        *,
        markets: Sequence[str] = PILOT_MARKETS,
        database_url: str | None = None,
        sim_start: datetime = DEFAULT_SIM_START,
        operator_delay: timedelta = DEFAULT_OPERATOR_DELAY,
        log_level: str = "WARNING",
    ) -> None:
        self.markets = tuple(markets)
        self.database_url = (
            database_url or os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///:memory:"
        )
        self.sim_start = sim_start
        self.operator_delay = operator_delay
        self.settings = pilot_settings(self.database_url, log_level=log_level)
        configure_logging(self.settings, force=True)
        self.sim = WorkflowSimulator(sim_start)
        self.gateway = SimTemporalGateway(self.sim)
        self.graph = MockGraphGateway(mailbox=PILOT_MAILBOX, clock=self.sim.now)
        self.provider = MockModelProvider()
        self.scripts = ScriptBook(self.provider)
        self.providers = mock_data_providers()
        self.personas: dict[str, Persona] = {}
        self.unbound: dict[str, str] = {}  # scenario id -> reason
        self._message_step: dict[str, tuple[str, int]] = {}  # message id -> (sequence id, step)
        self._sequence_persona: dict[str, Persona] = {}
        self._operator = _Operator()
        self._rng = random.Random(0)
        self._level = AutonomyLevel.L0
        self._approve_everything = True
        self.starter_skipped: dict[str, dict[str, int]] = {}  # market -> skip histogram
        self._app: FastAPI | None = None
        self._client: AsyncClient | None = None
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.m1_report: dict[str, Any] = {}
        self.a1_results: dict[str, dict[str, Any]] = {}
        self.delta_replay: dict[str, Any] = {}

    # -- entry point -----------------------------------------------------------------------------

    async def run(
        self,
        policy_level_overrides: dict[PolicyAction, AutonomyLevel] | None = None,
        approve_everything: bool = True,
        seed: int = 20261027,
    ) -> PilotReport:
        started = datetime.now(tz=UTC)
        t0 = time.perf_counter()
        self._rng = random.Random(seed)
        self._approve_everything = approve_everything
        overrides = dict(policy_level_overrides or {})
        self._level = overrides.get(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L0)
        scenarios = scenarios_for_markets(self.markets)
        self.scripts.install(scenarios)

        await self._prepare_database(overrides)
        assert self.session_factory is not None
        graph_acts.set_graph_context(
            graph_acts.GraphActivityContext(
                gateway=self.graph,
                session_factory=self.session_factory,
                settings=self.settings,
                clock=self.sim.now,
            )
        )
        conv_acts.set_conversation_context(
            conv_acts.ConversationContext(
                session_factory=self.session_factory,
                runner=AgentRunner(
                    self.provider, ModelRouter.from_settings(self.settings, MODEL_OVERRIDES)
                ),
                temporal=self.gateway,
                settings=self.settings,
                clock=self.sim.now,
                prompt_values={"company": self.settings.sender_company},
            )
        )
        discovery_acts.configure(self.session_factory, self.providers)
        starter_acts.set_starter_context(self._starter_context())
        self.sim.observers.append(self._observe_activity)
        try:
            await self._discover(scenarios)
            async with self._api() as client:
                self._client = client
                async with patched_workflow_modules(self.sim):
                    await self._schedule_sequence_starts()
                    await self.sim.run(on_idle=self._operator_turn)
                self.delta_replay = await self._replay_delta_poll()
                report = await self._collect(
                    scenarios, started=started, wall=time.perf_counter() - t0, seed=seed
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

    def _starter_context(self) -> starter_acts.StarterContext:
        assert self.session_factory is not None
        return starter_acts.StarterContext(
            session_factory=self.session_factory,
            temporal=self.gateway,
            settings=self.settings,
            clock=self.sim.now,
        )

    # -- phase 1: database ----------------------------------------------------------------------

    async def _prepare_database(self, overrides: dict[PolicyAction, AutonomyLevel]) -> None:
        import wholesaling.db.models  # noqa: F401 - register every table on Base

        self.engine = make_engine(self.database_url)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = make_session_factory(self.engine)
        async with self.session_factory() as s:
            repo = PolicyRepository(s)
            await repo.ensure_seeded(updated_by="pilot")  # what migration 0001 does: all L0
            for action, level in overrides.items():
                await repo.set_level(
                    action, level, updated_by="pilot", note="pilot policy_level_overrides"
                )
            await s.commit()

    # -- phase 2: discovery (M1, A1, A2) ------------------------------------------------------

    async def _discover(self, scenarios: list[Scenario]) -> None:
        assert self.session_factory is not None
        self.m1_report = await discovery_acts.run_m1(AS_OF.isoformat())
        async with self.session_factory() as s:
            markets = {m.name: m for m in (await s.execute(select(Market))).scalars().all()}
        for name in self.markets:
            market = markets.get(name)
            if market is None:
                msg = f"pilot market {name!r} is not one of M1's seed markets"
                raise LookupError(msg)
            self.a1_results[name] = await discovery_acts.run_a1(str(market.id), AS_OF.isoformat())
            for investor_id in self.a1_results[name].get("prospect_ids", []):
                # The starter runs on the simulated clock (phase 4), not inline here.
                await discovery_acts.run_a2(str(investor_id), start_sequence=False)
        async with self.session_factory() as s:
            for scenario in scenarios:
                if scenario.is_cohort_check:
                    continue
                market = markets.get(scenario.market or "")
                if market is None:
                    self.unbound[scenario.id] = f"market {scenario.market!r} not in the pilot"
                    continue
                inv_stmt = select(Investor).where(
                    Investor.market_id == market.id, Investor.canonical_name == scenario.investor
                )
                investor = (await s.execute(inv_stmt)).scalar_one_or_none()
                if investor is None:
                    self.unbound[scenario.id] = (
                        f"A1 did not produce investor {scenario.investor!r} in {market.name}"
                    )
                    continue
                contact_stmt = select(Contact).where(
                    Contact.investor_id == investor.id,
                    Contact.channel == ContactChannel.EMAIL,
                    Contact.is_primary.is_(True),
                )
                contact = (await s.execute(contact_stmt)).scalars().first()
                unreachable = (
                    await AuditRepository(s).get_by_idempotency_key(f"a2:{investor.id}:unreachable")
                    is not None
                )
                self.personas[scenario.id] = Persona(
                    scenario=scenario,
                    investor_id=investor.id,
                    investor_name=investor.canonical_name,
                    market=market.name,
                    score=investor.score,
                    email=contact.value if contact else None,
                    unreachable_recorded=unreachable,
                )
            # suppressed_before_start: an operator put the address on the list by hand.
            for persona in self.personas.values():
                if persona.scenario.suppress_before_start and persona.email:
                    s.add(
                        Suppression(
                            channel=Channel.EMAIL,
                            normalized_value=normalize_email(persona.email),
                            reason="manual",
                        )
                    )
            await s.commit()

    # -- phase 3: the API (review queue, webhook, policies) ------------------------------------

    def _api(self) -> _ApiSession:
        return _ApiSession(self)

    async def _post(self, path: str, **form: Any) -> dict[str, Any]:
        assert self._client is not None
        response = await self._client.post(
            path,
            data={k: v for k, v in form.items() if v is not None},
            headers={"accept": "application/json"},
        )
        if response.status_code >= 400:
            msg = f"POST {path} -> {response.status_code}: {response.text[:300]}"
            raise RuntimeError(msg)
        body: dict[str, Any] = response.json() if response.content else {}
        return body

    async def _set_levels(self, levels: dict[PolicyAction, AutonomyLevel], note: str) -> None:
        for action, level in levels.items():
            await self._post(f"/policies/{action.value}", level=level.value, note=note)

    async def _current_levels(
        self, actions: Iterable[PolicyAction]
    ) -> dict[PolicyAction, AutonomyLevel]:
        assert self.session_factory is not None
        async with self.session_factory() as s:
            repo = PolicyRepository(s)
            return {a: await repo.get_level(a) for a in actions}

    async def _scoped_levels(
        self, persona: Persona
    ) -> tuple[dict[PolicyAction, AutonomyLevel], dict[PolicyAction, AutonomyLevel]]:
        """(levels to force for this persona's workflow, levels to restore afterwards). A
        scope only ever raises the live level; it never lowers a global L1/L2 run."""
        if not persona.scenario.policy_scope:
            return {}, {}
        current = await self._current_levels(persona.scenario.policy_scope)
        forced = {
            action: persona.scenario.level_for(action, level) for action, level in current.items()
        }
        changed = {a: lvl for a, lvl in forced.items() if lvl is not current[a]}
        return changed, {a: current[a] for a in changed}

    # -- phase 4: sequences (the WF-02 starter on the simulated clock) ---------------------------

    async def _starter_candidates(self) -> list[tuple[Persona, starter_acts.StartCandidate]]:
        """The production starter's ordered candidates per pilot market (priority tier first),
        bound to their personas; the starter's skip histogram is kept for the report."""
        assert self.session_factory is not None
        by_investor = {p.investor_id: p for p in self.personas.values()}
        out: list[tuple[Persona, starter_acts.StartCandidate]] = []
        async with self.session_factory() as s:
            for name in self.markets:
                market = (await s.execute(select(Market).where(Market.name == name))).scalar_one()
                candidates, skipped = await starter_acts.list_startable_investors(s, market.id)
                self.starter_skipped[name] = skipped
                for candidate in candidates:
                    persona = by_investor.get(candidate.investor_id)
                    if persona is None:
                        self._operator.log.append(
                            {"unbound_candidate": candidate.to_json(), "market": name}
                        )
                        continue
                    out.append((persona, candidate))
            # Personas the starter left out: record the reason it would give.
            for persona in self.personas.values():
                if persona.investor_id in {c.investor_id for _, c in out}:
                    continue
                investor = await s.get(Investor, persona.investor_id)
                market_row = (
                    await s.get(Market, investor.market_id)
                    if investor is not None and investor.market_id
                    else None
                )
                if investor is not None and market_row is not None:
                    persona.starter_skip_reason = await starter_acts.skip_reason(
                        s, investor, market_row
                    )
        return out

    async def _schedule_sequence_starts(self) -> None:
        """One start event per candidate, two simulated minutes apart, in the starter's order:
        the same rows and workflow ids ``run_outreach_kickoff`` would create in one pass, but one
        workflow at a time so the simulation stays deterministic."""
        for index, (persona, candidate) in enumerate(await self._starter_candidates()):
            at = self.sim_start + SEQUENCE_START_SPACING * index

            async def _start(
                p: Persona = persona, c: starter_acts.StartCandidate = candidate, order: int = index
            ) -> None:
                await self._start_sequence(p, c, order)

            self.sim.schedule(at, _start, label=f"start:{persona.scenario.id}")

    async def _start_sequence(
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
        await self.sim.settle()
        if scope:
            await self._set_levels(saved, f"pilot scope restored after {persona.scenario.id}")

    # -- activity observation: touch timing, reply scheduling ----------------------------------

    async def _observe_activity(self, call: ActivityCall) -> None:
        assert self.session_factory is not None
        if call.name == "prepare_step":
            result = call.result
            self._message_step[result.message_id] = (call.arg.sequence_id, call.arg.step)
        elif call.name in ("decide_and_send", "send_approved") and call.result.sent:
            key = self._message_step.get(call.arg.message_id)
            if key is None:
                return
            sequence_id, step = key
            persona = self._sequence_persona.get(sequence_id)
            if persona is None or step in persona.touches:
                return
            persona.touches[step] = call.at
            if call.name == "decide_and_send":
                persona.auto_touches.add(step)
            self._schedule_replies(persona, touch=step + 1)

    # -- phase 5: scripted inbound ------------------------------------------------------------

    def _schedule_replies(self, persona: Persona, *, touch: int) -> None:
        for index, reply in enumerate(persona.scenario.replies):
            if reply.after_touch != touch or index in persona.scheduled_replies:
                continue
            persona.scheduled_replies.add(index)
            at = persona.touches[touch - 1] + timedelta(hours=reply.delay_hours)

            async def _inject(
                p: Persona = persona, i: int = index, r: ScriptedReply = reply
            ) -> None:
                await self._inject_reply(p, i, r)

            self.sim.schedule(at, _inject, label=f"inbound:{persona.scenario.id}#{index}")

    async def _inject_reply(self, persona: Persona, index: int, reply: ScriptedReply) -> None:
        assert self.session_factory is not None and persona.sequence_id is not None
        async with self.session_factory() as s:
            seq = await s.get(SequenceRow, persona.sequence_id)
            assert seq is not None and seq.thread_id is not None
            thread = await s.get(Thread, seq.thread_id)
            assert thread is not None
            stmt = (
                select(Message)
                .where(
                    Message.thread_id == thread.id,
                    Message.direction == Direction.OUT,
                    Message.sent_at.is_not(None),
                )
                .order_by(Message.sent_at)
            )
            sent = list((await s.execute(stmt)).scalars().all())
        our_subject = thread.subject or ""
        initial_body = sent[0].body if sent else ""
        last_internet_id = str(sent[-1].meta.get("internet_message_id") or "") if sent else None
        subject = (
            reply.subject.replace("{subject}", our_subject)
            if reply.subject is not None
            else f"RE: {our_subject}"
        )
        body = reply.body.replace("{subject}", our_subject).replace("{quoted}", initial_body)
        msg = self.graph.inject_inbound(
            from_address=reply.from_address or persona.email or "",
            subject=subject,
            body=body,
            conversation_id=thread.external_thread_id if reply.same_conversation else None,
            in_reply_to=last_internet_id if reply.same_conversation else None,
            headers=dict(reply.headers),
        )
        workflow_id = inbound_workflow_id(PILOT_SUBSCRIPTION, msg.graph_id)
        persona.injected.append(
            InjectedMessage(
                reply_index=index,
                graph_id=msg.graph_id,
                internet_message_id=msg.external_message_id,
                conversation_id=msg.conversation_id,
                at=self.sim.clock,
                workflow_id=workflow_id,
            )
        )
        scope, saved = await self._scoped_levels(persona)
        if scope:
            await self._set_levels(scope, f"pilot scope for {persona.scenario.id}")
        first = await self._notify_webhook(msg.graph_id)
        second = await self._notify_webhook(msg.graph_id)  # Graph delivers twice: no-op
        if first.get("accepted") != 1 or second.get("duplicates") != 1:
            self._operator.log.append(
                {"webhook_unexpected": {"first": first, "second": second, "graph_id": msg.graph_id}}
            )
        await self.sim.settle()
        if scope:
            await self._set_levels(saved, f"pilot scope restored after {persona.scenario.id}")

    async def _notify_webhook(self, graph_id: str) -> dict[str, Any]:
        assert self._client is not None
        response = await self._client.post(
            "/webhooks/graph",
            json={
                "value": [
                    {
                        "subscriptionId": PILOT_SUBSCRIPTION,
                        "clientState": PILOT_CLIENT_STATE,
                        "changeType": "created",
                        "resource": f"Users/{PILOT_MAILBOX}/Messages/{graph_id}",
                        "resourceData": {"id": graph_id},
                    }
                ]
            },
        )
        body: dict[str, Any] = response.json()
        body["status"] = response.status_code
        return body

    # -- phase 6: the operator -----------------------------------------------------------------

    async def _operator_turn(self) -> bool:
        """Called whenever every workflow is parked: approve what is queued, after a delay."""
        if not self._approve_everything or self._client is None:
            return False
        response = await self._client.get("/api/review", headers={"accept": "application/json"})
        response.raise_for_status()
        items = response.json()["items"]
        scheduled = False
        for item in items:
            message_id = item["message_id"]
            if message_id in self._operator.scheduled:
                continue
            self._operator.scheduled.add(message_id)
            scheduled = True

            async def _approve(mid: str = message_id, action: str = item["action"]) -> None:
                result = await self._post(f"/review/{mid}/approve")
                self._operator.approvals += 1
                if result.get("edited"):
                    self._operator.approvals_with_edits += 1
                self._operator.log.append(
                    {
                        "message_id": mid,
                        "action": action,
                        "at": self.sim.clock.isoformat(),
                        **result,
                    }
                )
                await self.sim.settle()

            self.sim.schedule_in(self.operator_delay, _approve, label=f"approve:{message_id}")
        return scheduled

    # -- phase 7: INV-24 replay -------------------------------------------------------------------

    async def _replay_delta_poll(self) -> dict[str, Any]:
        """Re-deliver every inbound message through the reconciliation path: zero new rows."""
        result = await graph_acts.delta_poll(graph_acts.DeltaPollInput(lookback_hours=24 * 60))
        return {
            "fetched": result.fetched,
            "created": result.created,
            "bounces": result.bounces,
        }

    # -- phase 8: collect ------------------------------------------------------------------------

    async def _collect(
        self, scenarios: list[Scenario], *, started: datetime, wall: float, seed: int
    ) -> PilotReport:
        assert self.session_factory is not None
        results: list[ScenarioResult] = []
        async with self.session_factory() as s:
            for scenario in scenarios:
                if scenario.is_cohort_check:
                    results.append(self._grade_cohort(scenario))
                    continue
                persona = self.personas.get(scenario.id)
                if persona is None:
                    results.append(
                        ScenarioResult(
                            scenario_id=scenario.id,
                            title=scenario.title,
                            market=scenario.market,
                            investor=scenario.investor,
                            status="skipped",
                            covers=scenario.covers,
                            skipped_reason=self.unbound.get(scenario.id, "not bound"),
                        )
                    )
                    continue
                results.append(await self._grade_persona(s, persona))
            aggregates, decisions, readiness = await self._aggregates(s)
            invariants = await self._invariants(s, results)
        gaps = self._gaps(aggregates)
        return PilotReport(
            mode="sim",
            level=self._level.value,
            approve_everything=self._approve_everything,
            seed=seed,
            markets=list(self.markets),
            started_at=started.isoformat(),
            finished_at=datetime.now(tz=UTC).isoformat(),
            wall_seconds=round(wall, 2),
            sim_start=self.sim_start.isoformat(),
            sim_end=self.sim.clock.isoformat(),
            simulated_days=round(self.sim.elapsed().total_seconds() / 86_400, 2),
            database_url=self.database_url,
            scenarios=results,
            invariants=invariants,
            aggregates=aggregates,
            readiness=readiness,
            policy_decisions=decisions,
            gaps=gaps,
            signal_contract_errors=[
                {
                    "workflow_id": e.workflow_id,
                    "signal": e.signal,
                    "payload_types": list(e.payload_types),
                    "handler_params": e.handler_params,
                    "error": e.error,
                }
                for e in self.sim.signal_contract_errors
            ],
            workflow_errors={k: f"{type(v).__name__}: {v}" for k, v in self.sim.errors.items()},
            unscripted_model_calls=list(self.scripts.unscripted),
            notes=self._notes(),
        )

    async def _persona_rows(self, s: AsyncSession, persona: Persona) -> dict[str, Any]:
        """Everything the grading needs for one persona, read back from the database."""
        seq = await s.get(SequenceRow, persona.sequence_id) if persona.sequence_id else None
        thread: Thread | None = None
        thread_messages: list[Message] = []
        if seq is not None and seq.thread_id is not None:
            thread = await s.get(Thread, seq.thread_id)
            stmt = select(Message).where(Message.thread_id == seq.thread_id).order_by(Message.id)
            thread_messages = list((await s.execute(stmt)).scalars().all())
        sequence_out = [
            m for m in thread_messages if m.direction == Direction.OUT and m.sequence_id is not None
        ]
        inbound: list[Message] = []
        for injected in persona.injected:
            stmt = select(Message).where(
                Message.channel == Channel.EMAIL,
                Message.external_message_id == injected.internet_message_id,
            )
            inbound.extend((await s.execute(stmt)).scalars().all())
        inbound_ids = {str(m.id) for m in inbound}
        reply_rows = [
            m
            for m in thread_messages
            if m.direction == Direction.OUT
            and m.sequence_id is None
            and str((m.meta or {}).get("reply_to_message_id")) in inbound_ids
            and not (m.meta or {}).get("supersedes")
        ]
        boxes = list(
            (
                await s.execute(
                    select(BuyBox)
                    .where(BuyBox.investor_id == persona.investor_id)
                    .order_by(BuyBox.buy_box_key, BuyBox.version)
                )
            )
            .scalars()
            .all()
        )
        suppression: Suppression | None = None
        if persona.email:
            sup_stmt = select(Suppression).where(
                Suppression.channel == Channel.EMAIL,
                Suppression.normalized_value == normalize_email(persona.email),
            )
            suppression = (await s.execute(sup_stmt)).scalar_one_or_none()
        correlation_ids = inbound_ids | {i.workflow_id for i in persona.injected}
        model_calls = 0
        if correlation_ids:
            model_calls = int(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(AgentRun)
                        .where(AgentRun.correlation_id.in_(list(correlation_ids)))
                    )
                ).scalar_one()
            )
        message_ids = [m.id for m in thread_messages]
        queued = 0
        if message_ids:
            audit_stmt = select(AuditEvent).where(
                AuditEvent.entity_type == "message",
                AuditEvent.entity_id.in_(message_ids),
                AuditEvent.event_type.in_(("outreach.policy_decided", "reply.decided")),
            )
            for event in (await s.execute(audit_stmt)).scalars().all():
                payload = event.payload or {}
                decision = payload.get("decision") or payload.get("policy_decision")
                if decision == PolicyDecision.QUEUE_FOR_HUMAN.value:
                    queued += 1
        return {
            "sequence": seq,
            "thread": thread,
            "sequence_out": sequence_out,
            "inbound": inbound,
            "reply_rows": reply_rows,
            "boxes": boxes,
            "suppression": suppression,
            "model_calls": model_calls,
            "queued": queued,
        }

    def _expected(self, scenario: Scenario) -> Expected:
        return scenario.expected(self._level, approve_everything=self._approve_everything)

    async def _grade_persona(self, s: AsyncSession, persona: Persona) -> ScenarioResult:
        scenario = persona.scenario
        expected: Expected = self._expected(scenario)
        rows = await self._persona_rows(s, persona)
        seq = rows["sequence"]
        sequence_out: list[Message] = rows["sequence_out"]
        inbound: list[Message] = rows["inbound"]
        reply_rows: list[Message] = rows["reply_rows"]
        boxes: list[BuyBox] = rows["boxes"]
        suppression: Suppression | None = rows["suppression"]

        sent_touches = [m for m in sequence_out if m.sent_at is not None]
        auto_touches = [m for m in sent_touches if not (m.meta or {}).get("human_approved")]
        replies_sent = [m for m in reply_rows if m.sent_at is not None]
        replies_auto = [
            m
            for m in replies_sent
            if (m.meta or {}).get("human_decision_applied") is None
            and (m.meta or {}).get("human_decision") is None
        ]
        current = {b.buy_box_key: b.version for b in boxes if b.is_current}
        corrections = sum(1 for b in boxes if (b.change_note or "").startswith("correction"))
        conv_ids = {
            str((m.meta or {}).get("conversation_id"))
            for m in [*sent_touches, *replies_sent]
            if (m.meta or {}).get("conversation_id")
        }
        thread = rows["thread"]
        same_conversation_inbound = [
            m
            for m, injected in zip(inbound, persona.injected, strict=False)
            if scenario.replies[injected.reply_index].same_conversation
        ]
        one_conversation = (
            len(conv_ids) <= 1
            and all(
                thread is not None and m.thread_id == thread.id for m in same_conversation_inbound
            )
            and (not conv_ids or thread is None or thread.external_thread_id in conv_ids)
        )

        checks = [
            Check("sequence_started", expected.sequence_started, seq is not None, True),
            Check(
                "sequence_status",
                expected.sequence_status,
                seq.status.value if seq is not None else None,
                True,
            ),
            Check(
                "cancel_reason",
                expected.cancel_reason,
                seq.cancel_reason if seq is not None else None,
                True,
            ),
            Check("touches_sent", expected.touches_sent, len(sent_touches), True),
            Check("auto_touches", expected.auto_touches, len(auto_touches), True),
            Check("inbound_rows", expected.inbound_rows, len(inbound), True),
            Check(
                "classifications",
                list(expected.classifications),
                [m.classification.value for m in inbound if m.classification is not None],
                True,
            ),
            Check("reply_drafts", expected.reply_drafts, len(reply_rows), True),
            Check("replies_sent", expected.replies_sent, len(replies_sent), True),
            Check("replies_auto_sent", expected.replies_auto_sent, len(replies_auto), True),
            Check("queued_items", expected.queued_items, rows["queued"], True),
            Check("buy_box_versions", dict(expected.buy_box_versions), current, True),
            Check("corrections", expected.corrections, corrections, True),
            Check(
                "suppression_reason",
                expected.suppression_reason,
                suppression.reason if suppression is not None else None,
                True,
            ),
            Check("model_calls", expected.model_calls, rows["model_calls"], True),
            Check("one_conversation", expected.one_conversation, one_conversation, True),
            Check("tier", expected.tier, persona.tier if persona.score is not None else None, True),
            Check(
                "starter_skip_reason",
                expected.starter_skip_reason,
                persona.starter_skip_reason,
                True,
            ),
            Check(
                "unreachable_recorded",
                expected.unreachable_recorded,
                persona.unreachable_recorded,
                True,
            ),
        ]
        # Per-message expectations (classification detail, stripping, dates, QA). Only the
        # first cancelling message finds an active sequence to signal; later ones find none.
        first_signal = next(
            (i for i, r in enumerate(scenario.replies) if r.expect.signal is not None), None
        )
        for m, injected in zip(inbound, persona.injected, strict=False):
            reply = scenario.replies[injected.reply_index]
            e = reply.expect
            expected_signal = e.signal if injected.reply_index == first_signal else None
            a4 = (m.meta or {}).get("a4") or {}
            prefix = f"inbound[{injected.reply_index}]"
            if reply.kind == "bounce":
                checks.append(
                    Check(f"{prefix}.is_bounce", True, bool((m.meta or {}).get("is_bounce")), True)
                )
                continue
            checks.append(
                Check(
                    f"{prefix}.is_auto_reply", e.is_auto_reply, bool(a4.get("is_auto_reply")), True
                )
            )
            checks.append(
                Check(
                    f"{prefix}.quoted_text_stripped",
                    e.quoted_text_stripped,
                    bool(a4.get("quoted_text_stripped")),
                    True,
                )
            )
            checks.append(
                Check(f"{prefix}.reconnect_date", e.reconnect_date, a4.get("reconnect_date"), True)
            )
            checks.append(Check(f"{prefix}.signalled", expected_signal, a4.get("signalled"), True))
            stored_reply = (m.meta or {}).get("reply") or {}
            qa_verdict = (stored_reply.get("qa") or {}).get("verdict")
            checks.append(Check(f"{prefix}.qa_verdict", e.qa_verdict, qa_verdict, True))
        for c in checks:
            c.ok = c.expected == c.actual

        failed = [c.name for c in checks if not c.ok]
        known = {gap for gap, names in scenario.known_defects.items() if set(failed) & set(names)}
        covered: set[str] = set()
        for gap in known:
            covered.update(scenario.known_defects[gap])
        status: Status
        if not failed:
            status = "pass"
        elif set(failed) <= covered:
            status = "known_defect"
        else:
            status = "fail"
        return ScenarioResult(
            scenario_id=scenario.id,
            title=scenario.title,
            market=persona.market,
            investor=persona.investor_name,
            status=status,
            checks=checks,
            covers=scenario.covers,
            known_defects=sorted(known),
            detail={
                "score": persona.score,
                "email": persona.email,
                "sequence_id": str(persona.sequence_id) if persona.sequence_id else None,
                "workflow_id": persona.workflow_id,
                "touches": {str(k): v.isoformat() for k, v in sorted(persona.touches.items())},
                "injected": [i.internet_message_id for i in persona.injected],
                "start_order": persona.start_order,
            },
        )

    def _grade_cohort(self, scenario: Scenario) -> ScenarioResult:
        """priority_before_prospect: within each market, every priority-tier persona's
        sequence started before every prospect's."""
        checks: list[Check] = []
        for market in self.markets:
            started = sorted(
                (
                    p
                    for p in self.personas.values()
                    if p.market == market and p.start_order is not None
                ),
                key=lambda p: p.start_order or 0,
            )
            tiers = [p.tier for p in started]
            first_prospect = next((i for i, t in enumerate(tiers) if t == "prospect"), len(tiers))
            ok = all(t == "prospect" for t in tiers[first_prospect:])
            checks.append(
                Check(
                    f"{market}: priority sequences start before prospects",
                    "priority* prospect*",
                    " ".join("P" if t == "priority" else "p" for t in tiers),
                    ok,
                )
            )
        status: Status = "pass" if checks and all(c.ok for c in checks) else "fail"
        return ScenarioResult(
            scenario_id=scenario.id,
            title=scenario.title,
            market=None,
            investor=None,
            status=status,
            checks=checks,
            covers=scenario.covers,
        )

    async def _aggregates(
        self, s: AsyncSession
    ) -> tuple[Aggregates, list[dict[str, Any]], list[dict[str, Any]]]:
        a = Aggregates()
        markets = (await s.execute(select(Market))).scalars().all()
        a.markets_scored = len(markets)
        a.markets_active = sum(1 for m in markets if m.tier is MarketTier.ACTIVE)
        a.markets_watch = sum(1 for m in markets if m.tier is MarketTier.WATCH)
        market_ids = [m.id for m in markets if m.name in self.markets]
        investors = (
            (await s.execute(select(Investor).where(Investor.market_id.in_(market_ids))))
            .scalars()
            .all()
        )
        a.investors_discovered = len(investors)
        qualified = [i for i in investors if i.excluded_reason is None and i.score is not None]
        a.investors_qualified = len(qualified)
        a.investors_priority = sum(1 for i in qualified if (i.score or 0) >= PRIORITY_SCORE)
        a.investors_with_email = sum(1 for p in self.personas.values() if p.email)

        sequences = (await s.execute(select(SequenceRow))).scalars().all()
        a.sequences_started = len(sequences)
        a.sequences_completed = sum(1 for q in sequences if q.status.value == "completed")
        a.sequences_cancelled = dict(
            Counter(q.cancel_reason or "?" for q in sequences if q.status.value == "cancelled")
        )
        outbound = (
            (await s.execute(select(Message).where(Message.direction == Direction.OUT)))
            .scalars()
            .all()
        )
        touches = [m for m in outbound if m.sequence_id is not None and m.sent_at is not None]
        a.touches_sent = len(touches)
        a.touches_sent_by_step = dict(Counter(str(m.sequence_step) for m in touches))
        a.touches_auto_sent = sum(1 for m in touches if not (m.meta or {}).get("human_approved"))
        a.subject_variants = dict(
            Counter(
                str((m.meta or {}).get("subject_variant")) for m in touches if m.sequence_step == 0
            )
        )
        inbound = (
            (await s.execute(select(Message).where(Message.direction == Direction.IN)))
            .scalars()
            .all()
        )
        a.inbound_injected = sum(len(p.injected) for p in self.personas.values())
        a.inbound_rows = len(inbound)
        a.inbound_audit_events = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "message.received")
                )
            ).scalar_one()
        )
        a.bounces = sum(1 for m in inbound if (m.meta or {}).get("is_bounce"))
        a.classifications = dict(
            Counter(m.classification.value for m in inbound if m.classification is not None)
        )
        a.auto_replies = sum(
            1 for m in inbound if ((m.meta or {}).get("a4") or {}).get("is_auto_reply")
        )
        boxes = (await s.execute(select(BuyBox))).scalars().all()
        a.buy_boxes_current = sum(1 for b in boxes if b.is_current)
        a.buy_box_versions = len(boxes)
        a.buy_box_corrections = sum(
            1 for b in boxes if (b.change_note or "").startswith("correction")
        )
        a.investors_with_buy_box = len({b.investor_id for b in boxes})
        suppressions = (await s.execute(select(Suppression))).scalars().all()
        a.suppressions = dict(Counter(x.reason for x in suppressions))
        replies = [
            m
            for m in outbound
            if m.sequence_id is None and (m.meta or {}).get("reply_to_message_id")
        ]
        a.reply_drafts = sum(1 for m in replies if not (m.meta or {}).get("supersedes"))
        a.replies_sent = sum(1 for m in replies if m.sent_at is not None)
        a.replies_auto_sent = sum(
            1
            for m in replies
            if m.sent_at is not None
            and (m.meta or {}).get("human_decision_applied") is None
            and (m.meta or {}).get("human_decision") is None
        )
        a.replies_blocked = sum(1 for m in replies if m.policy_decision is PolicyDecision.BLOCKED)
        a.queue_depth_end = sum(
            1 for m in outbound if m.draft and m.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
        )
        events = (await s.execute(select(AuditEvent))).scalars().all()
        a.approvals = sum(
            1 for e in events if e.event_type in (EVENT_APPROVED, EVENT_APPROVED_WITH_EDITS)
        )
        a.approvals_with_edits = sum(1 for e in events if e.event_type == EVENT_APPROVED_WITH_EDITS)
        a.rejections = sum(1 for e in events if e.event_type == EVENT_REJECTED)
        decisions: list[dict[str, Any]] = []
        hist: Counter[str] = Counter()
        queued_total = 0
        for e in events:
            p = e.payload or {}
            if e.event_type == "outreach.policy_decided":
                decisions.append(
                    {
                        "message_id": str(e.entity_id),
                        "action": p.get("action"),
                        "level": p.get("level"),
                        "decision": p.get("decision"),
                        "reason": p.get("reason"),
                    }
                )
                hist[f"{p.get('action')}:{p.get('decision')}"] += 1
                queued_total += int(p.get("decision") == PolicyDecision.QUEUE_FOR_HUMAN.value)
            elif e.event_type == "reply.decided":
                decisions.append(
                    {
                        "message_id": str(e.entity_id),
                        "action": p.get("policy_action"),
                        "level": p.get("policy_level"),
                        "decision": p.get("policy_decision"),
                        "reason": p.get("policy_reason"),
                        "qa_verdict": p.get("qa_verdict"),
                    }
                )
                hist[f"{p.get('policy_action')}:{p.get('policy_decision')}"] += 1
                queued_total += int(
                    p.get("policy_decision") == PolicyDecision.QUEUE_FOR_HUMAN.value
                )
        a.policy_decisions = dict(hist)
        a.queue_items_total = queued_total
        reviews = (await s.execute(select(QaReview))).scalars().all()
        a.qa_verdicts = dict(Counter(r.verdict.value for r in reviews))
        a.qa_flags = dict(Counter(f for r in reviews for f in (r.flags or [])))
        runs = (await s.execute(select(AgentRun))).scalars().all()
        a.agent_runs = len(runs)
        a.input_tokens = sum(r.input_tokens for r in runs)
        a.output_tokens = sum(r.output_tokens for r in runs)
        a.cost_usd = str(sum((r.cost_usd for r in runs), Decimal("0")))
        a.model_calls = len(self.provider.requests)
        a.model_calls_by_agent_task = dict(
            Counter(
                f"{r.metadata.agent.value}:{r.metadata.task_kind.value}"
                for r in self.provider.requests
            )
        )
        a.signal_contract_errors = len(self.sim.signal_contract_errors)
        a.workflows_started = len(self.sim.instances)
        a.workflows_failed = len(self.sim.errors)
        a.workflows_parked_at_end = len(self.sim.parked)
        readiness = [
            r.as_dict()
            for r in await all_promotion_readiness(s, now=self.sim.clock + timedelta(hours=1))
            if r.action in INVESTOR_ACTIONS
        ]
        return a, decisions, readiness

    async def _invariants(self, s: AsyncSession, results: list[ScenarioResult]) -> list[Check]:
        checks: list[Check] = []
        known = {r.scenario_id for r in results if r.status == "known_defect"}
        # INV-23: every sequence's sent touches share one conversation (the thread's).
        bad: list[str] = []
        for persona in self.personas.values():
            if persona.sequence_id is None:
                continue
            seq = await s.get(SequenceRow, persona.sequence_id)
            if seq is None or seq.thread_id is None:
                continue
            thread = await s.get(Thread, seq.thread_id)
            stmt = select(Message).where(
                Message.sequence_id == seq.id, Message.sent_at.is_not(None)
            )
            convs = {
                str((m.meta or {}).get("conversation_id"))
                for m in (await s.execute(stmt)).scalars().all()
            }
            if len(convs) > 1 or (
                convs and thread is not None and thread.external_thread_id not in convs
            ):
                bad.append(persona.scenario.id)
        checks.append(
            Check(
                "INV-23 all touches of a sequence in one Outlook conversation",
                [],
                bad,
                not bad,
            )
        )
        # INV-24: one inbound row and one received event per injected message, twice delivered.
        injected = sum(len(p.injected) for p in self.personas.values())
        rows = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.direction == Direction.IN)
                )
            ).scalar_one()
        )
        events = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.event_type == "message.received")
                )
            ).scalar_one()
        )
        checks.append(
            Check(
                "INV-24 one inbound row per injected message (webhook delivered twice)",
                injected,
                rows,
                rows == injected,
            )
        )
        checks.append(
            Check(
                "INV-24 one message.received audit event per inbound",
                injected,
                events,
                events == injected,
            )
        )
        checks.append(
            Check(
                "INV-24 delta-poll replay creates no rows",
                0,
                self.delta_replay.get("created"),
                self.delta_replay.get("created") == 0,
            )
        )
        # Webhook dedupe: every duplicate notification was reported as a duplicate.
        unexpected = [x for x in self._operator.log if "webhook_unexpected" in x]
        checks.append(
            Check("webhook: duplicate notification is a no-op", 0, len(unexpected), not unexpected)
        )
        # Every outbound send went through the gateway exactly once (RUL-05).
        sent_rows = int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.direction == Direction.OUT, Message.sent_at.is_not(None))
                )
            ).scalar_one()
        )
        checks.append(
            Check(
                "RUL-05 one mock send per sent outbound row",
                sent_rows,
                len(self.graph.sent),
                sent_rows == len(self.graph.sent),
            )
        )
        # Nothing left the mailbox for a suppressed address after suppression. ``created_at``
        # is wall-clock; the cut-off on the simulated timeline is the suppressing message's
        # ``received_at`` (opt-out / bounce) or the pilot start (manual, before any send).
        leaks = 0
        suppressions = (await s.execute(select(Suppression))).scalars().all()
        for sup in suppressions:
            cutoff = self.sim_start
            if sup.source_message_id is not None:
                source = await s.get(Message, sup.source_message_id)
                if source is not None and source.received_at is not None:
                    cutoff = _aware(source.received_at)
            stmt = select(Message).where(
                Message.direction == Direction.OUT, Message.sent_at.is_not(None)
            )
            for m in (await s.execute(stmt)).scalars().all():
                if m.sent_at is not None and _aware(m.sent_at) <= cutoff:
                    continue
                if any(
                    normalize_email(t) == sup.normalized_value for t in m.to_addresses if "@" in t
                ):
                    leaks += 1
        checks.append(
            Check("POL-03l no send to a suppressed address after suppression", 0, leaks, leaks == 0)
        )
        # Only the scripted L1 path auto-sends replies.
        expected_auto = sum(
            self._expected(p.scenario).replies_auto_sent
            for p in self.personas.values()
            if p.scenario.id not in known
        )
        auto_sent = 0
        for m in (
            await s.execute(select(Message).where(Message.direction == Direction.OUT))
        ).scalars():
            meta = m.meta or {}
            if (
                m.sequence_id is None
                and meta.get("reply_to_message_id")
                and m.sent_at is not None
                and meta.get("human_decision_applied") is None
                and meta.get("human_decision") is None
            ):
                auto_sent += 1
        checks.append(
            Check(
                "POL-03c replies auto-sent only where the scripted level allows",
                expected_auto,
                auto_sent,
                auto_sent == expected_auto,
            )
        )
        # Every model call is logged (XC-02 / RUL-05).
        runs = int((await s.execute(select(func.count()).select_from(AgentRun))).scalar_one())
        checks.append(
            Check(
                "XC-02 one agent_runs row per model call",
                len(self.provider.requests),
                runs,
                runs == len(self.provider.requests),
            )
        )
        checks.append(
            Check(
                "no workflow crashed",
                [],
                [f"{k}: {type(v).__name__}" for k, v in self.sim.errors.items()],
                not self.sim.errors,
            )
        )
        checks.append(
            Check(
                "no workflow left waiting without a timeout",
                [],
                self.sim.parked_without_timeout if self._approve_everything else [],
                not self.sim.parked_without_timeout or not self._approve_everything,
            )
        )
        return checks

    def _gaps(self, a: Aggregates) -> list[Gap]:
        """Open gaps the run evidences. GAP-01..05 and GAP-07 from the first pilot are fixed
        (BO-09 follow-up; see docs/pilot.md); GAP-01 is kept as a regression guard that only
        appears when a signal payload is rejected again."""
        gaps: list[Gap] = []
        if a.signal_contract_errors:
            gaps.append(
                Gap(
                    id="GAP-01",
                    severity="high",
                    where=(
                        "activities/conversation.py `_signal_sequence` vs "
                        "workflows/investor_sequence.py `reply`/`opt_out`/`bounce`"
                    ),
                    behaviour=(
                        "A sequence signal carried a payload the handler could not accept "
                        "(regression of the fixed GAP-01: both sides must import the payload "
                        "dataclasses from wholesaling/signals.py)."
                    ),
                    expected="Zero signal contract errors.",
                    evidence=f"{a.signal_contract_errors} contract errors in this run (see list).",
                )
            )
        gaps.append(
            Gap(
                id="GAP-06",
                severity="low",
                where="agents/a4.py `SENSITIVE_CONTENT_RE`",
                behaviour=(
                    "Any dollar figure or `150k`-style number in a *reply draft* flags it "
                    "sensitive, so an L1 auto-reply that merely restates the investor's own price "
                    "cap queues."
                ),
                expected=(
                    "Intended conservatism (POL-03c), documented in docs/agents/conversation.md; "
                    "drafts should avoid restating figures. No change required."
                ),
                evidence=(
                    f"QA flags this run: {a.qa_flags or {}}; "
                    f"replies auto-sent {a.replies_auto_sent}."
                ),
            )
        )
        return gaps

    def _notes(self) -> list[str]:
        notes = [
            "Mock model, mock Graph, mock Temporal: no real mailbox, no real model, no real "
            "server.",
            f"M1: {len(self.m1_report.get('rows', []))} markets scored; pilot markets are Watch "
            "tier (no Active until a human approves M1-11).",
        ]
        for name, result in self.a1_results.items():
            notes.append(
                f"A1 {name}: deeds {result.get('deeds_pulled')}, found {result.get('found')}, "
                f"qualified {result.get('qualified')}, priority {result.get('priority')}"
            )
        if self.unbound:
            notes.extend(f"unbound scenario {k}: {v}" for k, v in self.unbound.items())
        notes.append(
            f"operator: {self._operator.approvals} approvals through POST /review/{{id}}/approve "
            f"({self._operator.approvals_with_edits} with edits), delay {self.operator_delay}"
        )
        notes.append(f"delta poll replay: {self.delta_replay}")
        for market, skipped in self.starter_skipped.items():
            notes.append(f"WF-02 starter {market}: skipped {skipped or {}}")
        return notes

    async def _record_report(self, report: PilotReport) -> None:
        """Keep the report in the pilot database too, so GET /pilot/report can serve it."""
        assert self.session_factory is not None
        async with self.session_factory() as s:
            await AuditRepository(s).record(
                actor=Actor.SYSTEM,
                actor_id="pilot",
                event_type="pilot.completed",
                entity_type="pilot",
                payload=report.to_dict(),
            )
            await s.commit()


class _ApiSession:
    """The FastAPI app over the pilot's own engine and gateway, as an httpx client."""

    def __init__(self, runner: PilotRunner) -> None:
        self._runner = runner
        self._app: FastAPI | None = None
        self._client: AsyncClient | None = None
        self._lifespan: Any = None

    async def __aenter__(self) -> AsyncClient:
        runner = self._runner
        app = create_app(runner.settings)
        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()
        await app.state.engine.dispose()  # the lifespan's own engine; we share the pilot's
        app.state.engine = runner.engine
        app.state.session_factory = runner.session_factory
        app.state.temporal = runner.gateway
        self._app = app
        self._client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://pilot",
            auth=BasicAuth(OPERATOR, OPERATOR_PASSWORD),
        )
        return await self._client.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)
        if self._app is not None:
            # Give the lifespan back its own engine so its shutdown does not dispose ours.
            self._app.state.engine = make_engine("sqlite+aiosqlite:///:memory:")
        if self._lifespan is not None:
            await self._lifespan.__aexit__(None, None, None)


__all__ = [
    "AS_OF",
    "DEFAULT_SIM_START",
    "INVESTOR_ACTIONS",
    "OPERATOR",
    "OPERATOR_PASSWORD",
    "PILOT_CLIENT_STATE",
    "PILOT_MAILBOX",
    "PILOT_SUBSCRIPTION",
    "SEQUENCE_WORKFLOW",
    "InjectedMessage",
    "Persona",
    "PilotRunner",
    "ScriptBook",
    "pilot_settings",
]
