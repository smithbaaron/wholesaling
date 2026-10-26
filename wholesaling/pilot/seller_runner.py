"""``SellerPilotRunner``: the whole seller loop on SQLite in mock mode, without a Temporal server
(REQUIREMENTS.md BO-14, BO-20).

    fresh database -> policies at L0 (or the seller actions at L1) -> M1 -> the operator makes
    Houston Active (the PRE-17 human approval) -> A1 + A2 on Houston (the buyers S6 will blast;
    three of them get a buy box) -> S1 + S2 through ``activities.seller.refresh_market`` (the
    weekly refresh with ``start_sequences=False``) -> three hand-entered leads (GAP-S01) -> the
    production starter ``start_seller_sequence`` on the simulated clock, two minutes apart ->
    SellerSequenceWorkflow steps (A5 QA, PolicyEngine(seller_outreach_email)) -> scripted
    seller emails injected into the mock Graph gateway and announced twice through POST
    /webhooks/graph -> GraphInboundMessageWorkflow routes the seller thread to its child
    SellerInboundWorkflow (S4 classify -> intake -> reply | OfferWorkflow) -> homeowners POST the
    real /sell form -> OfferWorkflow (A7 V1..V6, A5 QA, PolicyEngine(offer_send)) ->
    NegotiationWorkflow with scripted accepts / counters and the operator's overrides -> the
    operator's clicks on the REAL /deals routes (PSA, mark signed, earnest money, buyer response,
    select buyer, deposit, closed / dead) -> DispositionWorkflow (S6 blast, deadline alerts) ->
    collect -> grade -> SellerPilotReport

Everything the production worker would run is the production code. The pilot adds the
simulator (``sim.py``), the mock model's scripted outputs (``SellerScriptBook``), two fixture
work-arounds it reports as gaps (property-fixture parcels registered with the mock deeds
provider, GAP-S02; A7 fixture subjects aliased under the leads' addresses, GAP-S02), the three
seeded leads (GAP-S01), the S5 router it plays for post-offer replies (GAP-S04) and two
engine-level injections (a V6 drift comp set and a V5 recompute bug, the technique of
``tests/agents/a7/test_engine.py``).
"""

from __future__ import annotations

import dataclasses
import os
import random
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from types import TracebackType
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, BasicAuth
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from wholesaling.activities import conversation as conv_acts
from wholesaling.activities import discovery as discovery_acts
from wholesaling.activities import disposition as disp_acts
from wholesaling.activities import graph as graph_acts
from wholesaling.activities import offers as offer_acts
from wholesaling.activities import seller as seller_acts
from wholesaling.agents.a4 import MODEL_OVERRIDES as A4_MODEL_OVERRIDES
from wholesaling.agents.a7 import engine as a7_engine_mod
from wholesaling.agents.a7 import mao as a7_mao
from wholesaling.agents.a7.audit import MODEL_OVERRIDES as A7_MODEL_OVERRIDES
from wholesaling.agents.llm.mock import MockModelProvider, ScriptedResponse
from wholesaling.agents.llm.models import ModelRequest, TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.agents.templates.investor_outreach import SenderIdentity
from wholesaling.agents.templates.seller_outreach import missing_compliance_blocks
from wholesaling.api.main import create_app
from wholesaling.api.routes.review import EVENT_APPROVED, EVENT_APPROVED_WITH_EDITS
from wholesaling.config.tunables import get_tunables
from wholesaling.db import Base, make_engine, make_session_factory
from wholesaling.db.models import (
    AgentRun,
    AuditEvent,
    Contact,
    Investor,
    Market,
    Message,
    QaReview,
    Suppression,
    Thread,
)
from wholesaling.db.models.deals import Contract, ContractType
from wholesaling.db.models.enums import (
    Actor,
    AgentId,
    Channel,
    ContactChannel,
    Direction,
    MarketTier,
    OfferStatus,
    PolicyDecision,
    SellerLeadStatus,
)
from wholesaling.db.models.messaging import Sequence as SequenceRow
from wholesaling.db.models.offers import Offer, OfferVerification
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.normalize import normalize_email
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.buy_boxes import BuyBoxCriteria, BuyBoxRepository
from wholesaling.db.repositories.markets import MarketRepository
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.db.repositories.sellers import SellerRepository
from wholesaling.integrations.blob import MockBlobStorage
from wholesaling.integrations.data.mock import MockDeedRecordsProvider, mock_data_providers
from wholesaling.integrations.data.models import ParcelRecord
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    MockPropertyDataProvider,
    PropertyAddress,
    SoldComp,
    SubjectFixture,
    mock_property_provider,
)
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.logging import configure_logging
from wholesaling.pilot.report import Check, Gap, ScenarioResult, Status
from wholesaling.pilot.runner import (
    OPERATOR,
    OPERATOR_PASSWORD,
    PILOT_CLIENT_STATE,
    PILOT_MAILBOX,
    PILOT_SUBSCRIPTION,
    pilot_settings,
)
from wholesaling.pilot.seller_report import SellerAggregates, SellerPilotReport
from wholesaling.pilot.seller_scenarios import (
    GAP_S03,
    GAP_S04,
    GAP_S05,
    GAP_S08,
    GAP_S09,
    GAP_S11,
    GAP_S12,
    PERSONA_SCENARIOS,
    SELLER_LEVEL_ACTIONS,
    SELLER_MARKET,
    SELLER_SCENARIOS,
    SELLER_TEMPLATE_QA,
    WATCH_MARKET,
    PropertyAlias,
    S4Script,
    SellerMessage,
    SellerScenario,
)
from wholesaling.pilot.sim import (
    ActivityCall,
    DeferredStartSimGateway,
    SellerWorkflowSimulator,
    patched_workflow_modules_for,
)
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.reporting.kpis import all_promotion_readiness
from wholesaling.workflows.negotiation import (
    AcceptSignal,
    CounterSignal,
    DeclineSignal,
    HumanOverride,
    negotiation_workflow_id,
)

# Tuesday 2026-10-27 10:00 America/Chicago: inside the send window, same start as BO-09.
DEFAULT_SIM_START = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
AS_OF = datetime(2026, 10, 25, tzinfo=UTC)
DEFAULT_OPERATOR_DELAY = timedelta(hours=1)
SEQUENCE_START_SPACING = timedelta(minutes=2)
SIM_HORIZON = timedelta(days=120)  # decline -> requeue at +90 d must fit
WEBHOOK_PATH = "/webhooks/graph"
PHOTO_BYTES = b"\xff\xd8\xff\xe0pilot-photo\xff\xd9"
EARNEST_MONEY = "2500"
ASSIGNMENT_FEE = "10000"
BUYER_DEPOSIT = "5000"
TITLE_COMPANY = "Pilot Title Co"
DEAL_DEAD_REASON = "inspection: foundation movement not disclosed"
HUMAN_CONTRACT_EVENTS = (
    "contract.created",
    "contract.sent_for_signature",
    "contract.signed",
    "contract.earnest_money",
    "contract.deadlines_set",
    "contract.buyer_selected",
    "contract.deposit_received",
    "contract.closed",
    "contract.dead",
)
SELLER_ACTIONS = (
    PolicyAction.SELLER_OUTREACH_EMAIL,
    PolicyAction.SELLER_INTAKE_REPLY,
    PolicyAction.OFFER_SEND,
    PolicyAction.COUNTER_ACCEPTANCE,
    PolicyAction.BUYER_DISPOSITION_BLAST,
)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _latest_section(request: ModelRequest) -> str:
    content = "\n".join(m.content for m in request.messages)
    return content.rsplit("LATEST INBOUND MESSAGE", 1)[-1]


def _draft_section(request: ModelRequest) -> str:
    content = "\n".join(m.content for m in request.messages)
    return content.rsplit("DRAFT BODY:", 1)[-1]


# --------------------------------------------------------------------------------------------
# Runtime binding of a scenario to a seller lead
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class InjectedEmail:
    message_index: int
    graph_id: str
    internet_message_id: str
    at: datetime
    workflow_id: str


@dataclass(slots=True)
class SellerPersona:
    scenario: SellerScenario
    lead_id: uuid.UUID | None = None
    email: str | None = None
    sequence_id: uuid.UUID | None = None
    sequence_workflow_id: str | None = None
    start_order: int | None = None
    submission_id: str | None = None
    form_lead_created: bool | None = None
    form_message_id: str | None = None
    offer_id: uuid.UUID | None = None
    touches: dict[int, datetime] = field(default_factory=dict)
    auto_touches: set[int] = field(default_factory=set)
    s4_replies_sent: list[datetime] = field(default_factory=list)
    offer_sent_at: datetime | None = None
    offer_auto: bool | None = None
    scheduled_messages: set[int] = field(default_factory=set)
    injected: list[InjectedEmail] = field(default_factory=list)
    negotiation_signals: list[str] = field(default_factory=list)
    override_scheduled: bool = False
    contract_requested_at: datetime | None = None
    deal_scheduled: bool = False
    contract_id: uuid.UUID | None = None
    blast_recipients: list[str] = field(default_factory=list)
    drift_injected: bool = False


@dataclass(slots=True)
class _Operator:
    approvals: int = 0
    approval_failures: int = 0
    scheduled: set[str] = field(default_factory=set)
    log: list[dict[str, Any]] = field(default_factory=list)
    deals: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Model scripting
# --------------------------------------------------------------------------------------------


class SellerScriptBook:
    """Scripted S4 / A5 / A7-audit outputs on the mock model; unscripted calls are listed."""

    def __init__(self, provider: MockModelProvider) -> None:
        self.provider = provider
        self.unscripted: list[str] = []
        self.hits: Counter[str] = Counter()

    def install(self, scenarios: Iterable[SellerScenario]) -> None:
        for scenario in scenarios:
            for index, message in enumerate(scenario.messages):
                self._install_message(scenario, index, message)
        for needle, verdict in SELLER_TEMPLATE_QA.items():
            self.provider.script_matching(
                self._a5_predicate(needle), self._responder("template", TaskKind.QA, verdict)
            )
        self.provider.script_matching(lambda _r: True, self._unscripted)

    def _install_message(
        self, scenario: SellerScenario, index: int, message: SellerMessage
    ) -> None:
        tag = f"{scenario.id}#{index}"
        script: S4Script = message.model
        for kind, parsed in (
            (TaskKind.CLASSIFY, script.classify),
            (TaskKind.EXTRACT, script.extract),
            (TaskKind.DRAFT, script.draft),
        ):
            if parsed is None:
                continue
            self.provider.script_matching(
                self._s4_predicate(kind, message.match), self._responder(tag, kind, parsed)
            )
        if script.qa is not None and script.draft is not None:
            self.provider.script_matching(
                self._a5_predicate(str(script.draft["body"])[:60]),
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
    def _s4_predicate(kind: TaskKind, match: str) -> Callable[[ModelRequest], bool]:
        def _pred(r: ModelRequest) -> bool:
            return (
                r.metadata.agent is AgentId.S4
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
        agent = request.metadata.agent
        if kind is TaskKind.COMP_AUDIT:
            # The deterministic pre-filters carry V3; the model adds nothing in the pilot.
            self.hits["a7:comp_audit"] += 1
            return ScriptedResponse(
                parsed={"verdicts": [], "rationale": "pilot: pre-filter verdicts stand"}
            )
        excerpt = _latest_section(request).strip().replace("\n", " ")[:80]
        self.unscripted.append(f"{agent.value}:{kind.value} <- {excerpt!r}")
        if kind is TaskKind.CLASSIFY:
            label = "unrelated" if agent is AgentId.S4 else "ambiguous"
            parsed: dict[str, Any] = {
                "classification": label,
                "confidence": 0.5,
                "rationale": "unscripted pilot message",
                "is_auto_reply": False,
            }
            if agent is AgentId.S4:
                parsed["asks_offer"] = False
            else:
                parsed["reconnect_date"] = None
            return ScriptedResponse(parsed=parsed)
        if kind is TaskKind.EXTRACT:
            if agent is AgentId.S4:
                from wholesaling.pilot.seller_scenarios import s4_extract

                return ScriptedResponse(parsed=s4_extract("unscripted"))
            return ScriptedResponse(parsed={"buy_boxes": [], "rationale": "unscripted"})
        if kind is TaskKind.DRAFT:
            return ScriptedResponse(
                parsed={
                    "subject": "",
                    "body": "Thanks for your note - could you tell me a little more?",
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
# Fixture work-arounds (reported as gaps)
# --------------------------------------------------------------------------------------------


def alias_subject(
    provider: MockPropertyDataProvider,
    alias: PropertyAlias,
    target: PropertyAddress,
    *,
    suffix: str,
) -> SubjectFixture:
    """Serve the fixture subject at ``alias`` under ``target``: parcel ids suffixed so two leads
    never share one (``no_active_offer_on_parcel``), comps re-zipped so the step-2 query matches."""
    source = provider.subject(
        PropertyAddress(alias.fixture_line1, "Houston", "TX", alias.fixture_postal_code)
    )
    if source is None:
        msg = f"no A7 fixture subject at {alias.fixture_line1}"
        raise LookupError(msg)

    def _facts(facts: Any) -> Any:
        if facts is None:
            return None
        return dataclasses.replace(
            facts, parcel_id=f"{facts.parcel_id}-{suffix}" if facts.parcel_id else None
        )

    listing = source.listing
    if listing is not None and listing.parcel_id:
        listing = dataclasses.replace(listing, parcel_id=f"{listing.parcel_id}-{suffix}")
    copy = SubjectFixture(
        key=target.key,
        county=_facts(source.county),
        provider=_facts(source.provider),
        third=_facts(source.third),
        avm=source.avm,
        listing=listing,
        comps=[
            dataclasses.replace(c, postal_code=target.postal_code or c.postal_code)
            for c in source.comps
        ],
    )
    provider.subjects[target.key] = copy
    return copy


def register_parcel(deeds: Any, address: PropertyAddress, parcel_id: str) -> None:
    """Teach the mock deeds provider one more parcel so S4 can verify the address (GAP-S02)."""
    parcels: list[ParcelRecord] | None = getattr(deeds, "parcels", None)
    if parcels is None:
        return
    parcels.append(
        ParcelRecord(
            parcel_id=parcel_id,
            address=address.line1,
            city=address.city or "Houston",
            state=address.state or "TX",
            zip=address.postal_code or "",
            property_type="sfr",
            units=1,
            source="pilot_property_fixture",
            source_ref=parcel_id,
        )
    )


def drift_comps(postal_code: str) -> list[SoldComp]:
    """Three recent, close sales at $170/sqft (tests/agents/a7/test_engine.py ``_inject``)."""
    return [
        SoldComp(
            cid,
            f"P-{cid}",
            f"{cid} New St",
            postal_code,
            Decimal(170 * 1500),
            DEFAULT_TODAY,
            1500,
            Decimal("0.10"),
            year_built=1980,
        )
        for cid in ("NEW1", "NEW2", "NEW3")
    ]


def buggy_compute_mao(inputs: a7_mao.MaoInputs) -> a7_mao.MaoResult:
    """The engine's MAO step with the offer off by one rounding step (what V5 must catch)."""
    res = a7_mao.compute_mao(inputs)
    return dataclasses.replace(res, offer=res.offer + Decimal(500))


# --------------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------------


class SellerPilotRunner:
    """Run the synthetic seller pilot once. ``run()`` returns a ``SellerPilotReport``."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        sim_start: datetime = DEFAULT_SIM_START,
        operator_delay: timedelta = DEFAULT_OPERATOR_DELAY,
        log_level: str = "WARNING",
    ) -> None:
        self.database_url = (
            database_url or os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///:memory:"
        )
        self.sim_start = sim_start
        self.operator_delay = operator_delay
        self.settings = pilot_settings(self.database_url, log_level=log_level)
        configure_logging(self.settings, force=True)
        self.sim = SellerWorkflowSimulator(sim_start, horizon=SIM_HORIZON)
        self.gateway = DeferredStartSimGateway(self.sim)
        self.graph = MockGraphGateway(mailbox=PILOT_MAILBOX, clock=self.sim.now)
        self.provider = MockModelProvider()
        self.scripts = SellerScriptBook(self.provider)
        self.providers = mock_data_providers()
        self.property_provider = mock_property_provider()
        self.runner = AgentRunner(
            self.provider,
            ModelRouter.from_settings(self.settings, {**A4_MODEL_OVERRIDES, **A7_MODEL_OVERRIDES}),
        )
        self.personas: dict[str, SellerPersona] = {}
        self.unbound: dict[str, str] = {}
        self._by_lead: dict[uuid.UUID, SellerPersona] = {}
        self._by_sequence: dict[str, SellerPersona] = {}
        self._by_offer: dict[str, SellerPersona] = {}
        self._message_step: dict[str, tuple[str, int]] = {}
        self._operator = _Operator()
        self._rng = random.Random(0)
        self._level = AutonomyLevel.L0
        self._approve_everything = True
        self._client: AsyncClient | None = None
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.m1_report: dict[str, Any] = {}
        self.a1_result: dict[str, Any] = {}
        self.refresh: dict[str, Any] = {}
        self.buyers: list[uuid.UUID] = []
        self._v5_patched = False
        self._market_id: uuid.UUID | None = None

    # -- entry point -----------------------------------------------------------------------------

    async def run(
        self,
        level: AutonomyLevel = AutonomyLevel.L0,
        approve_everything: bool = True,
        seed: int = 20261027,
    ) -> SellerPilotReport:
        started = datetime.now(tz=UTC)
        t0 = time.perf_counter()
        self._rng = random.Random(seed)
        self._level = level
        self._approve_everything = approve_everything
        self.scripts.install(SELLER_SCENARIOS)
        self._register_fixture_parcels()

        await self._prepare_database(level)
        assert self.session_factory is not None
        self._install_contexts()
        self.sim.observers.append(self._observe_activity)
        self.sim.pre_observers.append(self._before_activity)
        try:
            await self._discover()
            await self._seller_lists()
            async with _ApiSession(self) as client:
                self._client = client
                async with patched_workflow_modules_for(self.sim):
                    await self._schedule_sequence_starts()
                    self._schedule_forms()
                    await self.sim.run(on_idle=self._operator_turn)
                report = await self._collect(
                    started=started, wall=time.perf_counter() - t0, seed=seed
                )
            await self._record_report(report)
            return report
        finally:
            self._client = None
            self._uninstall_contexts()
            if self._v5_patched:
                a7_engine_mod.compute_mao = a7_mao.compute_mao  # type: ignore[attr-defined]
                self._v5_patched = False
            if self.engine is not None:
                await self.engine.dispose()

    # -- wiring -----------------------------------------------------------------------------------

    def _install_contexts(self) -> None:
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
                runner=self.runner,
                temporal=self.gateway,
                settings=self.settings,
                clock=self.sim.now,
                prompt_values={"company": self.settings.sender_company},
            )
        )
        discovery_acts.configure(self.session_factory, self.providers)
        seller_acts.set_seller_context(
            seller_acts.SellerContext(
                session_factory=self.session_factory,
                providers=self.providers,
                temporal=self.gateway,
                settings=self.settings,
                clock=self.sim.now,
                runner=self.runner,
                prompt_values={"company": self.settings.sender_company},
            )
        )
        offer_acts.set_offer_context(
            offer_acts.OfferActivityContext(
                property_provider=self.property_provider,
                runner=self.runner,
                tunables=get_tunables(),
                prompt_values={"company": self.settings.sender_company},
            )
        )
        disp_acts.set_disposition_context(
            disp_acts.DispositionContext(
                session_factory=self.session_factory,
                settings=self.settings,
                gateway=self.graph,
                clock=self.sim.now,
            )
        )

    @staticmethod
    def _uninstall_contexts() -> None:
        disp_acts.set_disposition_context(None)
        offer_acts.set_offer_context(None)
        seller_acts.set_seller_context(None)
        discovery_acts.reset()
        conv_acts.set_conversation_context(None)
        graph_acts.set_graph_context(None)

    def _register_fixture_parcels(self) -> None:
        """GAP-S02: the A7 fixture addresses (and the aliases) are unknown to the mock deeds
        provider, so a web form on them would never parcel-verify; register them here."""
        deeds = self.providers.deeds
        assert isinstance(deeds, MockDeedRecordsProvider)
        for line1, postal_code in FIXTURE_SUBJECTS:
            address = PropertyAddress(line1, "Houston", "TX", postal_code)
            subject = self.property_provider.subject(address)
            facts = (subject.county or subject.provider) if subject is not None else None
            register_parcel(deeds, address, (facts.parcel_id if facts else None) or line1)
        for scenario in PERSONA_SCENARIOS:
            if scenario.alias is None or scenario.address_line1 is None:
                continue
            target = PropertyAddress(
                scenario.address_line1, "Houston", "TX", scenario.postal_code or ""
            )
            copy = alias_subject(
                self.property_provider, scenario.alias, target, suffix=scenario.id[:12]
            )
            if scenario.source == "web_form":
                facts = copy.county or copy.provider
                register_parcel(deeds, target, (facts.parcel_id if facts else None) or scenario.id)

    # -- phase 1: database ----------------------------------------------------------------------

    async def _prepare_database(self, level: AutonomyLevel) -> None:
        import wholesaling.db.models  # noqa: F401 - register every table on Base

        self.engine = make_engine(self.database_url)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = make_session_factory(self.engine)
        async with self.session_factory() as s:
            repo = PolicyRepository(s)
            await repo.ensure_seeded(updated_by="pilot")
            if level is not AutonomyLevel.L0:
                for action in SELLER_LEVEL_ACTIONS:
                    await repo.set_level(action, level, updated_by="pilot", note="seller pilot")
            await s.commit()

    # -- phase 2: M1, the PRE-17 approval, A1 + A2 (buyers) ------------------------------------

    async def _discover(self) -> None:
        assert self.session_factory is not None
        self.m1_report = await discovery_acts.run_m1(AS_OF.isoformat())
        async with self.session_factory() as s:
            repo = MarketRepository(s)
            market = await repo.get_by_name_state(SELLER_MARKET, "TX")
            if market is None:
                msg = f"{SELLER_MARKET} is not one of M1's seed markets"
                raise LookupError(msg)
            # PRE-17 / M1-11: a human promotes the pilot market to Active.
            await repo.update_tier(market, MarketTier.ACTIVE, crossings=repo.crossings(market))
            await AuditRepository(s).record(
                actor=Actor.HUMAN,
                actor_id=OPERATOR,
                event_type="market.tier_approved",
                entity_type="market",
                entity_id=market.id,
                payload={"tier": MarketTier.ACTIVE.value, "note": "seller pilot (PRE-17)"},
            )
            await s.commit()
            self._market_id = market.id
        self.a1_result = await discovery_acts.run_a1(str(self._market_id), AS_OF.isoformat())
        for investor_id in self.a1_result.get("prospect_ids", []):
            await discovery_acts.run_a2(str(investor_id), start_sequence=False)
        async with self.session_factory() as s:
            stmt = (
                select(Investor)
                .join(Contact, Contact.investor_id == Investor.id)
                .where(
                    Investor.market_id == self._market_id,
                    Contact.channel == ContactChannel.EMAIL,
                    Contact.is_primary.is_(True),
                    Investor.excluded_reason.is_(None),
                )
                .order_by(Investor.score.desc(), Investor.canonical_name)
            )
            investors = list((await s.execute(stmt)).scalars().unique().all())[:3]
            boxes = BuyBoxRepository(s)
            for inv in investors:
                # The buy box an A4 conversation would have produced in the investor pilot;
                # entered here by the operator so S6 has buyers to blast (tier 2 fit).
                await boxes.create_version(
                    inv.id,
                    BuyBoxCriteria(
                        geography={"markets": ["Houston, TX"]},
                        asset_types=["sfr"],
                        price_min=Decimal(40000),
                        price_max=Decimal(350000),
                        condition="any",
                        strategy="flip",
                        raw_text="seller pilot: operator-entered buy box",
                    ),
                    source_ref="seller-pilot",
                    change_note="seller pilot seed",
                )
                self.buyers.append(inv.id)
            await s.commit()

    # -- phase 3: S1 + S2 (the weekly refresh) and the seeded leads ----------------------------

    async def _seller_lists(self) -> None:
        assert self.session_factory is not None and self._market_id is not None
        ctx = seller_acts.get_seller_context()
        async with self.session_factory() as s:
            houston = await s.get(Market, self._market_id)
            columbus = await MarketRepository(s).get_by_name_state(WATCH_MARKET, "OH")
            assert houston is not None and columbus is not None
        result = await seller_acts.refresh_market(
            ctx, houston, seller_acts.SellerListRefreshInput(start_sequences=False)
        )
        self.refresh[SELLER_MARKET] = dataclasses.asdict(result)
        refused = await seller_acts.refresh_market(
            ctx, columbus, seller_acts.SellerListRefreshInput(start_sequences=False)
        )
        self.refresh[WATCH_MARKET] = dataclasses.asdict(refused)
        async with self.session_factory() as s:
            for scenario in PERSONA_SCENARIOS:
                if scenario.seed is None:
                    continue
                seed = scenario.seed
                address = PropertyAddress(seed.address_line1, "Houston", "TX", seed.postal_code)
                lead = SellerLead(
                    market_id=self._market_id,
                    status=SellerLeadStatus.QUEUED,
                    owner_name=seed.owner_name,
                    property_address_line1=seed.address_line1,
                    property_city="Houston",
                    property_state="TX",
                    property_postal_code=seed.postal_code,
                    address_key=address.key,
                    parcel_id=f"PILOT-SEED-{scenario.id}",
                    parcel_verified=True,
                    signals=["operator_entered"],
                    signal_score=seed.signal_score,
                    source="pilot_seed",
                    source_refs={"pilot": {"scenario": scenario.id, "gap": "GAP-S01"}},
                    contact_channels=[
                        {
                            "channel": "email",
                            "value": seed.email,
                            "normalized_value": normalize_email(seed.email),
                            "source": "pilot_seed",
                            "provenance": "operator-entered (seller pilot)",
                            "confidence": 1.0,
                            "is_primary": True,
                            "dnc_listed": False,
                            "suppressed": False,
                        }
                    ],
                )
                s.add(lead)
            await s.commit()
        await self._bind_leads()

    async def _bind_leads(self) -> None:
        assert self.session_factory is not None
        async with self.session_factory() as s:
            leads = {
                lead.owner_name: lead
                for lead in (await s.execute(select(SellerLead))).scalars().all()
            }
        for scenario in PERSONA_SCENARIOS:
            if scenario.source == "web_form":
                persona = SellerPersona(scenario=scenario, email=scenario.form.email)  # type: ignore[union-attr]
                self.personas[scenario.id] = persona
                continue
            lead = leads.get(scenario.owner or "")
            if lead is None:
                self.unbound[scenario.id] = f"S1/S2 produced no lead for {scenario.owner!r}"
                continue
            persona = SellerPersona(
                scenario=scenario, lead_id=lead.id, email=SellerRepository.primary_email(lead)
            )
            self.personas[scenario.id] = persona
            self._by_lead[lead.id] = persona

    # -- phase 4: sequences through the production starter -----------------------------------------

    async def _schedule_sequence_starts(self) -> None:
        assert self.session_factory is not None and self._market_id is not None
        async with self.session_factory() as s:
            repo = SellerRepository(s)
            leads = await repo.list_by_status(self._market_id, SellerLeadStatus.QUEUED)
            candidates: list[tuple[SellerLead, str]] = []
            for lead in leads:
                email = SellerRepository.primary_email(lead)
                if email is None or lead.id not in self._by_lead:
                    continue
                candidates.append((lead, email))
        for index, (lead, email) in enumerate(candidates):
            at = self.sim_start + SEQUENCE_START_SPACING * index

            async def _start(
                lead_: SellerLead = lead, email_: str = email, order: int = index
            ) -> None:
                await self._start_sequence(lead_, email_, order)

            self.sim.schedule(at, _start, label=f"start:{lead.owner_name}")

    async def _start_sequence(self, lead: SellerLead, email: str, order: int) -> None:
        ctx = seller_acts.get_seller_context()
        workflow_id = await seller_acts.start_seller_sequence(
            ctx, lead, contact_email=email, market=SELLER_MARKET
        )
        persona = self._by_lead[lead.id]
        persona.sequence_workflow_id = workflow_id
        persona.sequence_id = uuid.UUID(workflow_id.split(":", 1)[1])
        persona.start_order = order
        self._by_sequence[str(persona.sequence_id)] = persona
        await self.sim.settle()

    # -- phase 5: the public web form --------------------------------------------------------------

    def _schedule_forms(self) -> None:
        for persona in self.personas.values():
            form = persona.scenario.form
            if form is None:
                continue

            async def _post(p: SellerPersona = persona) -> None:
                await self._post_form(p)

            self.sim.schedule(
                self.sim_start + timedelta(hours=form.at_hours),
                _post,
                label=f"form:{persona.scenario.id}",
            )

    async def _post_form(self, persona: SellerPersona) -> None:
        assert self._client is not None and self.session_factory is not None
        form = persona.scenario.form
        assert form is not None
        submission_id = str(uuid.UUID(int=self._rng.getrandbits(128)))
        data = {
            "submission_id": submission_id,
            "address_line1": form.address_line1,
            "city": form.city,
            "state": form.state,
            "postal_code": form.postal_code,
            "name": form.name,
            "email": form.email,
            "phone": form.phone,
            "motivation": form.motivation,
            "condition_class": form.condition_class,
            "disclosed_issues": form.disclosed_issues,
            "timeline": form.timeline,
            "asking_price": form.asking_price,
            "consent_email": "yes",
        }
        files = [
            ("photos", (f"photo{i}.jpg", PHOTO_BYTES, "image/jpeg")) for i in range(form.photos)
        ]
        response = await self._client.post(
            "/sell", data=data, files=files or None, headers={"x-forwarded-for": "203.0.113.7"}
        )
        if response.status_code >= 400:
            self._operator.log.append(
                {"form_rejected": persona.scenario.id, "status": response.status_code}
            )
            return
        persona.submission_id = submission_id
        async with self.session_factory() as s:
            lead = await SellerRepository(s).get_by_submission_id(submission_id)
            if lead is None:
                # Merged into an existing lead (same address): the submission id went there.
                stmt = select(AuditEvent).where(
                    AuditEvent.idempotency_key == f"form:{submission_id}:submitted"
                )
                event = (await s.execute(stmt)).scalar_one_or_none()
                lead = (
                    await s.get(SellerLead, event.entity_id)
                    if event is not None and event.entity_id is not None
                    else None
                )
            if lead is None:
                self.unbound[persona.scenario.id] = "POST /sell created no lead"
                return
            event = (
                await s.execute(
                    select(AuditEvent).where(
                        AuditEvent.idempotency_key == f"form:{submission_id}:submitted"
                    )
                )
            ).scalar_one_or_none()
            persona.form_lead_created = (
                bool((event.payload or {}).get("lead_created")) if event is not None else None
            )
            persona.form_message_id = (
                str((event.payload or {}).get("message_id")) if event is not None else None
            )
        if lead.id not in self._by_lead:
            persona.lead_id = lead.id
            self._by_lead[lead.id] = persona
        else:
            persona.lead_id = lead.id  # merged into another persona's lead (dedupe scenario)
        await self.sim.settle()

    # -- activity observation ---------------------------------------------------------------------

    async def _before_activity(self, workflow_id: str, name: str, arg: Any) -> None:
        if name == "compute_offer":
            persona = self._by_lead.get(uuid.UUID(arg.seller_lead_id))
            if persona is not None and persona.scenario.inject_v5_mismatch:
                a7_engine_mod.compute_mao = buggy_compute_mao  # type: ignore[attr-defined]
                self._v5_patched = True
        elif name == "pre_send_check":
            persona = self._by_offer.get(arg.offer_id)
            if (
                persona is not None
                and persona.scenario.inject_drift_comps
                and not persona.drift_injected
                and persona.scenario.address_line1 is not None
            ):
                persona.drift_injected = True
                address = PropertyAddress(
                    persona.scenario.address_line1, "Houston", "TX", persona.scenario.postal_code
                )
                for comp in drift_comps(persona.scenario.postal_code or ""):
                    self.property_provider.inject_comp(address, comp)

    async def _observe_activity(self, call: ActivityCall) -> None:
        name, arg, result = call.name, call.arg, call.result
        if name == "compute_offer" and self._v5_patched:
            a7_engine_mod.compute_mao = a7_mao.compute_mao  # type: ignore[attr-defined]
            self._v5_patched = False
        if name == "seller_prepare_step":
            self._message_step[result.message_id] = (arg.sequence_id, arg.step)
        elif name in ("seller_decide_and_send", "seller_send_approved") and result.sent:
            key = self._message_step.get(arg.message_id)
            if key is None:
                return
            sequence_id, step = key
            persona = self._by_sequence.get(sequence_id)
            if persona is None or step in persona.touches:
                return
            persona.touches[step] = call.at
            if name == "seller_decide_and_send":
                persona.auto_touches.add(step)
            self._schedule_messages(persona, "touch", step + 1)
        elif name in ("seller_intake", "seller_apply_human_decision") and result.sent:
            lead_id = getattr(result, "seller_lead_id", None)
            persona = (
                self._by_lead.get(uuid.UUID(lead_id))
                if lead_id
                else await self._persona_for_inbound(arg.message_id)
            )
            if persona is None:
                return
            persona.s4_replies_sent.append(call.at)
            self._schedule_messages(persona, "s4_reply", len(persona.s4_replies_sent))
        elif name == "compute_offer":
            persona = self._by_lead.get(uuid.UUID(arg.seller_lead_id))
            if persona is not None:
                persona.offer_id = uuid.UUID(result.offer_id)
                self._by_offer[result.offer_id] = persona
        elif name in ("decide_and_send_offer", "send_approved_offer") and result.sent:
            persona = self._by_offer.get(arg.offer_id)
            if persona is None or persona.offer_sent_at is not None:
                return
            persona.offer_sent_at = call.at
            persona.offer_auto = name == "decide_and_send_offer"
            self._schedule_messages(persona, "offer_sent", 1)
        elif name == "evaluate_counter":
            persona = self._by_offer.get(arg.offer_id)
            if persona is None:
                return
            if result.accepted:
                self._schedule_deal(persona, call.at)
            elif result.decision == PolicyDecision.QUEUE_FOR_HUMAN.value:
                self._schedule_override(persona)
        elif name == "accept_offer":
            persona = self._by_offer.get(arg.offer_id)
            if persona is not None:
                self._schedule_deal(persona, call.at)
        elif name == "run_blast":
            contract_id = uuid.UUID(arg.contract_id)
            for persona in self.personas.values():
                if persona.contract_id == contract_id:
                    persona.blast_recipients = [r.investor_id for r in result.recipients]

    async def _persona_for_inbound(self, message_id: str) -> SellerPersona | None:
        assert self.session_factory is not None
        async with self.session_factory() as s:
            row = await s.get(Message, uuid.UUID(message_id))
            thread = await s.get(Thread, row.thread_id) if row is not None else None
        if thread is None or thread.seller_lead_id is None:
            return None
        return self._by_lead.get(thread.seller_lead_id)

    # -- phase 6: scripted seller emails ---------------------------------------------------------

    def _schedule_messages(self, persona: SellerPersona, trigger: str, index: int) -> None:
        for i, message in enumerate(persona.scenario.messages):
            if (
                message.trigger != trigger
                or message.index != index
                or i in persona.scheduled_messages
            ):
                continue
            persona.scheduled_messages.add(i)

            async def _inject(
                p: SellerPersona = persona, idx: int = i, m: SellerMessage = message
            ) -> None:
                await self._inject_email(p, idx, m)

            self.sim.schedule_in(
                timedelta(hours=message.delay_hours),
                _inject,
                label=f"inbound:{persona.scenario.id}#{i}",
            )

    async def _inject_email(
        self, persona: SellerPersona, index: int, message: SellerMessage
    ) -> None:
        assert self.session_factory is not None and persona.lead_id is not None
        async with self.session_factory() as s:
            thread = await SellerRepository(s).thread_for_lead(persona.lead_id)
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
        subject = f"RE: {thread.subject or ''}".strip()
        last_internet_id = str(sent[-1].meta.get("internet_message_id") or "") if sent else None
        msg = self.graph.inject_inbound(
            from_address=persona.email or "",
            subject=subject,
            body=message.body,
            conversation_id=thread.external_thread_id,
            in_reply_to=last_internet_id,
        )
        persona.injected.append(
            InjectedEmail(
                message_index=index,
                graph_id=msg.graph_id,
                internet_message_id=msg.external_message_id,
                at=self.sim.clock,
                workflow_id=f"seller-inbound:email:{msg.graph_id}",
            )
        )
        first = await self._notify_webhook(msg.graph_id)
        second = await self._notify_webhook(msg.graph_id)
        if first.get("accepted") != 1 or second.get("duplicates") != 1:
            self._operator.log.append(
                {"webhook_unexpected": {"first": first, "second": second, "graph_id": msg.graph_id}}
            )
        await self.sim.settle()
        if message.negotiation is not None:
            await self._play_negotiation_move(persona, message)

    async def _notify_webhook(self, graph_id: str) -> dict[str, Any]:
        assert self._client is not None
        response = await self._client.post(
            WEBHOOK_PATH,
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

    # -- phase 7: S5 (the pilot plays the router the inbound pipeline lacks, GAP-S04) -------------

    async def _play_negotiation_move(self, persona: SellerPersona, message: SellerMessage) -> None:
        assert self.session_factory is not None and message.negotiation is not None
        move = message.negotiation
        if persona.offer_id is None:
            self._operator.log.append({"negotiation_move_without_offer": persona.scenario.id})
            return
        wf_id = negotiation_workflow_id(str(persona.offer_id))
        inbound_id = persona.injected[-1].internet_message_id if persona.injected else None
        payload: Any
        if move.kind == "accept":
            payload = AcceptSignal(message_id=inbound_id)
        elif move.kind == "decline":
            payload = DeclineSignal(reason="seller declined by email", message_id=inbound_id)
        else:
            async with self.session_factory() as s:
                offer = await s.get(Offer, persona.offer_id)
            assert offer is not None and offer.offer_price is not None and offer.mao is not None
            room = offer.mao - offer.offer_price
            amount = (offer.offer_price + room * Decimal(str(move.room_fraction))).quantize(
                Decimal("100"), rounding=ROUND_DOWN
            )
            payload = CounterSignal(amount=str(amount), message_id=inbound_id)
        try:
            await self.gateway.signal(wf_id, move.kind, payload)
            persona.negotiation_signals.append(move.kind)
        except Exception as exc:
            self._operator.log.append(
                {"negotiation_signal_failed": persona.scenario.id, "error": str(exc)[:200]}
            )
        await self.sim.settle()

    def _schedule_override(self, persona: SellerPersona) -> None:
        """The counter waits for a human (L0 or above MAO): the operator decides after a delay."""
        if persona.override_scheduled or not self._approve_everything:
            return
        move = next(
            (m.negotiation for m in persona.scenario.messages if m.negotiation is not None), None
        )
        if move is None or move.human_override is None:
            return
        persona.override_scheduled = True

        async def _override(p: SellerPersona = persona, action: str = move.human_override) -> None:
            assert p.offer_id is not None and self.session_factory is not None
            amount: str | None = None
            if action == "accept":
                async with self.session_factory() as s:
                    offer = await s.get(Offer, p.offer_id)
                counters = list((offer.calc_record if offer else {}).get("counters") or [])
                amount = str(counters[-1]["amount"]) if counters else None
            override = HumanOverride(
                action=action,
                amount=amount,
                reason=(
                    "operator accepted the counter inside the room"
                    if action == "accept"
                    else "seller wants more than the numbers support"
                ),
                actor_id=OPERATOR,
            )
            await self.gateway.signal(
                negotiation_workflow_id(str(p.offer_id)), "human_override", override
            )
            p.negotiation_signals.append(f"human_override:{action}")
            self._operator.log.append(
                {
                    "human_override": action,
                    "scenario": p.scenario.id,
                    "at": self.sim.clock.isoformat(),
                }
            )
            await self.sim.settle()

        self.sim.schedule_in(
            self.operator_delay, _override, label=f"override:{persona.scenario.id}"
        )

    # -- phase 8: the human on /deals --------------------------------------------------------------

    def _schedule_deal(self, persona: SellerPersona, at: datetime) -> None:
        if persona.deal_scheduled:
            return
        persona.deal_scheduled = True
        persona.contract_requested_at = at
        plan = persona.scenario.deal_plan
        if plan is None or not self._approve_everything:
            return

        async def _psa(p: SellerPersona = persona) -> None:
            await self._deal_psa_and_sign(p)

        self.sim.schedule_in(timedelta(hours=1), _psa, label=f"psa:{persona.scenario.id}")

    async def _deals_post(self, path: str, **form: Any) -> dict[str, Any] | None:
        assert self._client is not None
        response = await self._client.post(
            path,
            data={k: v for k, v in form.items() if v is not None},
            headers={"accept": "application/json"},
        )
        body: dict[str, Any] = response.json() if response.content else {}
        body["status_code"] = response.status_code
        self._operator.deals.append({"path": path, "at": self.sim.clock.isoformat(), **body})
        if response.status_code >= 400:
            return None
        return body

    async def _deal_psa_and_sign(self, persona: SellerPersona) -> None:
        assert persona.offer_id is not None
        created = await self._deals_post(
            f"/deals/offers/{persona.offer_id}/psa",
            title_company=TITLE_COMPANY,
            earnest_money_amount=EARNEST_MONEY,
        )
        if created is None:
            return
        contract_id = uuid.UUID(str(created["contract_id"]))
        persona.contract_id = contract_id

        async def _sign(p: SellerPersona = persona, cid: uuid.UUID = contract_id) -> None:
            now = self.sim.clock
            signed = await self._deals_post(
                f"/deals/{cid}/signed",
                signed_at=now.isoformat(),
                envelope_id=str(created.get("envelope_id") or ""),
                option_period_end=(now + timedelta(days=7)).isoformat(),
                inspection_deadline=(now + timedelta(days=10)).isoformat(),
                closing_date=(now + timedelta(days=21)).isoformat(),
                title_company=TITLE_COMPANY,
            )
            await self.sim.settle()
            if signed is None:
                return
            await self._deals_post(
                f"/deals/{cid}/earnest", status="requested", amount=EARNEST_MONEY
            )
            await self.sim.settle()
            plan = p.scenario.deal_plan
            if plan == "close":
                self.sim.schedule_in(
                    timedelta(days=2),
                    lambda: self._deal_close_steps(p),
                    label=f"buyer:{p.scenario.id}",
                )
            elif plan == "dead_after_inspection":
                self.sim.schedule_in(
                    timedelta(days=2), lambda: self._deal_earnest_deposited(p), label="earnest"
                )
                self.sim.schedule_in(
                    timedelta(days=11), lambda: self._deal_dead(p), label=f"dead:{p.scenario.id}"
                )

        self.sim.schedule_in(timedelta(days=1), _sign, label=f"sign:{persona.scenario.id}")

    async def _deal_close_steps(self, persona: SellerPersona) -> None:
        cid = persona.contract_id
        assert cid is not None
        buyer = (
            persona.blast_recipients[0]
            if persona.blast_recipients
            else (str(self.buyers[0]) if self.buyers else None)
        )
        if buyer is None:
            self._operator.log.append({"no_buyer_for_deal": persona.scenario.id})
            return
        await self._deals_post(
            f"/deals/{cid}/buyer-response",
            investor_id=buyer,
            response="interested",
            proof_of_funds="yes",
            deposit_terms="5k non-refundable at title",
        )
        await self.sim.settle()
        await self._deals_post(
            f"/deals/{cid}/select-buyer",
            investor_id=buyer,
            assignment_fee=ASSIGNMENT_FEE,
            deposit_amount=BUYER_DEPOSIT,
        )
        await self.sim.settle()
        await self._deals_post(f"/deals/{cid}/earnest", status="deposited", amount=EARNEST_MONEY)
        await self.sim.settle()

        async def _deposit(p: SellerPersona = persona) -> None:
            await self._deals_post(
                f"/deals/{cid}/deposit",
                amount=BUYER_DEPOSIT,
                received_at=self.sim.clock.isoformat(),
            )
            await self.sim.settle()

        async def _close(p: SellerPersona = persona) -> None:
            await self._deals_post(
                f"/deals/{cid}/closed",
                realized_assignment_fee=ASSIGNMENT_FEE,
                closed_at=self.sim.clock.isoformat(),
            )
            await self.sim.settle()

        self.sim.schedule_in(timedelta(days=1), _deposit, label=f"deposit:{persona.scenario.id}")
        self.sim.schedule_in(timedelta(days=19), _close, label=f"close:{persona.scenario.id}")

    async def _deal_earnest_deposited(self, persona: SellerPersona) -> None:
        assert persona.contract_id is not None
        await self._deals_post(
            f"/deals/{persona.contract_id}/earnest", status="deposited", amount=EARNEST_MONEY
        )
        await self.sim.settle()

    async def _deal_dead(self, persona: SellerPersona) -> None:
        assert persona.contract_id is not None
        await self._deals_post(f"/deals/{persona.contract_id}/dead", reason=DEAL_DEAD_REASON)
        await self.sim.settle()

    # -- phase 9: the operator on the review queue -------------------------------------------------

    async def _operator_turn(self) -> bool:
        if not self._approve_everything or self._client is None:
            return False
        response = await self._client.get("/api/review", headers={"accept": "application/json"})
        response.raise_for_status()
        scheduled = False
        for item in response.json()["items"]:
            message_id = item["message_id"]
            if message_id in self._operator.scheduled:
                continue
            self._operator.scheduled.add(message_id)
            scheduled = True

            async def _approve(mid: str = message_id, action: str = item["action"]) -> None:
                assert self._client is not None
                result = await self._client.post(
                    f"/review/{mid}/approve", headers={"accept": "application/json"}
                )
                entry: dict[str, Any] = {
                    "message_id": mid,
                    "action": action,
                    "at": self.sim.clock.isoformat(),
                    "status": result.status_code,
                }
                if result.status_code >= 400:
                    self._operator.approval_failures += 1
                    entry["detail"] = result.text[:300]
                else:
                    self._operator.approvals += 1
                self._operator.log.append(entry)
                await self.sim.settle()

            self.sim.schedule_in(self.operator_delay, _approve, label=f"approve:{message_id}")
        return scheduled

    # -- phase 10: collect -----------------------------------------------------------------------

    async def _collect(self, *, started: datetime, wall: float, seed: int) -> SellerPilotReport:
        assert self.session_factory is not None
        results: list[ScenarioResult] = []
        async with self.session_factory() as s:
            for scenario in SELLER_SCENARIOS:
                if scenario.is_cohort_check:
                    results.append(self._grade_cohort(scenario))
                    continue
                persona = self.personas.get(scenario.id)
                if persona is None or persona.lead_id is None:
                    results.append(
                        ScenarioResult(
                            scenario_id=scenario.id,
                            title=scenario.title,
                            market=SELLER_MARKET,
                            investor=scenario.owner,
                            status="skipped",
                            covers=scenario.covers,
                            skipped_reason=self.unbound.get(scenario.id, "not bound"),
                        )
                    )
                    continue
                results.append(await self._grade_persona(s, persona))
            aggregates, readiness = await self._aggregates(s)
            invariants = await self._invariants(s)
        return SellerPilotReport(
            mode="sim",
            level=self._level.value,
            approve_everything=self._approve_everything,
            seed=seed,
            market=SELLER_MARKET,
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
            gaps=self._gaps(aggregates),
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
            operator_log=list(self._operator.log) + list(self._operator.deals),
            notes=self._notes(),
        )

    async def _persona_rows(self, s: AsyncSession, persona: SellerPersona) -> dict[str, Any]:
        assert persona.lead_id is not None
        lead = await s.get(SellerLead, persona.lead_id)
        assert lead is not None
        repo = SellerRepository(s)
        intakes = list(
            (await s.execute(select(Intake).where(Intake.seller_lead_id == lead.id))).scalars()
        )
        sequence = (
            await s.get(SequenceRow, persona.sequence_id)
            if persona.sequence_id is not None
            else None
        )
        thread_ids = [
            t.id
            for t in (
                await s.execute(select(Thread).where(Thread.seller_lead_id == lead.id))
            ).scalars()
        ]
        messages = (
            list(
                (
                    await s.execute(
                        select(Message)
                        .where(Message.thread_id.in_(thread_ids))
                        .order_by(Message.created_at, Message.id)
                    )
                ).scalars()
            )
            if thread_ids
            else []
        )
        inbound = [m for m in messages if m.direction == Direction.IN]
        touches = [
            m
            for m in messages
            if m.direction == Direction.OUT and m.sequence_id is not None and m.sent_at is not None
        ]
        s4_replies = [
            m
            for m in messages
            if m.direction == Direction.OUT
            and m.policy_action == PolicyAction.SELLER_INTAKE_REPLY.value
            and not (m.meta or {}).get("supersedes")
        ]
        offers = list(
            (
                await s.execute(
                    select(Offer).where(Offer.seller_lead_id == lead.id).order_by(Offer.created_at)
                )
            ).scalars()
        )
        offer = offers[-1] if offers else None
        verifications: list[OfferVerification] = []
        if offer is not None:
            verifications = list(
                (
                    await s.execute(
                        select(OfferVerification)
                        .where(OfferVerification.offer_id == offer.id)
                        .order_by(OfferVerification.created_at, OfferVerification.iteration)
                    )
                ).scalars()
            )
        contract = (
            await s.execute(
                select(Contract).where(
                    Contract.seller_lead_id == lead.id, Contract.type == ContractType.PSA
                )
            )
        ).scalar_one_or_none()
        events = list(
            (await s.execute(select(AuditEvent).order_by(AuditEvent.created_at))).scalars()
        )
        suppression: Suppression | None = None
        if persona.email:
            suppression = (
                await s.execute(
                    select(Suppression).where(
                        Suppression.channel == Channel.EMAIL,
                        Suppression.normalized_value == normalize_email(persona.email),
                    )
                )
            ).scalar_one_or_none()
        leads_for_address = 0
        if lead.address_key:
            leads_for_address = int(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(SellerLead)
                        .where(SellerLead.address_key == lead.address_key)
                    )
                ).scalar_one()
            )
        return {
            "lead": lead,
            "intakes": intakes,
            "sequence": sequence,
            "inbound": inbound,
            "touches": touches,
            "s4_replies": s4_replies,
            "offer": offer,
            "verifications": verifications,
            "contract": contract,
            "events": events,
            "suppression": suppression,
            "leads_for_address": leads_for_address,
            "repo": repo,
        }

    async def _grade_persona(self, s: AsyncSession, persona: SellerPersona) -> ScenarioResult:
        scenario = persona.scenario
        expected = scenario.expected(self._level)
        rows = await self._persona_rows(s, persona)
        lead: SellerLead = rows["lead"]
        sequence: SequenceRow | None = rows["sequence"]
        inbound: list[Message] = rows["inbound"]
        touches: list[Message] = rows["touches"]
        s4_replies: list[Message] = rows["s4_replies"]
        offer: Offer | None = rows["offer"]
        verifications: list[OfferVerification] = rows["verifications"]
        contract: Contract | None = rows["contract"]
        events: list[AuditEvent] = rows["events"]
        suppression: Suppression | None = rows["suppression"]
        intakes: list[Intake] = rows["intakes"]
        intake = intakes[-1] if intakes else None

        skip_trace = (lead.source_refs or {}).get("skip_trace") or {}
        s4_sent = [m for m in s4_replies if m.sent_at is not None]
        s4_auto = [
            m
            for m in s4_sent
            if (m.meta or {}).get("human_decision_applied") is None
            and (m.meta or {}).get("human_decision") is None
        ]
        s4_queued = sum(
            1
            for e in events
            if e.event_type == "intake.reply_decided"
            and str((e.payload or {}).get("seller_lead_id")) == str(lead.id)
            and (e.payload or {}).get("policy_decision") == PolicyDecision.QUEUE_FOR_HUMAN.value
        )
        record = offer.calc_record if offer is not None else {}
        gates_failed = sorted(
            g["name"] for g in (offer.gates if offer is not None else []) if not g.get("passed")
        )
        passes = sorted({v.pass_id.value for v in verifications})
        by_pass = {
            p: [v.result.value for v in verifications if v.pass_id.value == p] for p in passes
        }
        offer_sent_event = next(
            (
                e
                for e in events
                if e.event_type == "offer.sent" and offer is not None and e.entity_id == offer.id
            ),
            None,
        )
        contract_requested = any(
            e.event_type == "contract.requested" and offer is not None and e.entity_id == offer.id
            for e in events
        )
        accepted_event = next(
            (
                e
                for e in events
                if e.event_type == "contract.requested"
                and offer is not None
                and e.entity_id == offer.id
            ),
            None,
        )
        counters = list(record.get("counters") or [])
        negotiation_result = (
            self.sim.results.get(negotiation_workflow_id(str(offer.id))) if offer else None
        )
        disposition_result = (
            self.sim.results.get(f"disposition:{contract.id}") if contract is not None else None
        )
        alert_raised = any(
            e.event_type == "alert.raised"
            and (e.payload or {}).get("kind") == "a7.recompute_mismatch"
            and offer is not None
            and e.entity_id == offer.id
            for e in events
        )
        release_reminder = any(
            e.event_type == "alert.raised"
            and (e.payload or {}).get("kind") == "earnest_money_release"
            and contract is not None
            and e.entity_id == contract.id
            for e in events
        )
        blast_rows = (
            [
                m
                for m in (await s.execute(select(Message))).scalars()
                if contract is not None
                and str((m.meta or {}).get("contract_id")) == str(contract.id)
                and m.policy_action == PolicyAction.BUYER_DISPOSITION_BLAST.value
            ]
            if contract is not None
            else []
        )
        offer_rows = (
            [
                m
                for m in (await s.execute(select(Message))).scalars()
                if offer is not None and str((m.meta or {}).get("offer_id")) == str(offer.id)
            ]
            if offer is not None
            else []
        )
        v6_offer_changed = any(
            v.pass_id.value == "V6" and v.result.value == "adjusted" for v in verifications
        )
        actual: dict[str, Any] = {
            "lead_found": True,
            "lead_source": lead.source,
            "lead_status": lead.status.value,
            "parcel_verified": lead.parcel_verified,
            "dnc_listed": bool(skip_trace.get("dnc_listed")),
            "sequence_status": sequence.status.value if sequence is not None else None,
            "cancel_reason": sequence.cancel_reason if sequence is not None else None,
            "touches_sent": len(touches),
            "auto_touches": sum(1 for m in touches if not (m.meta or {}).get("human_approved")),
            "inbound_rows": len(inbound),
            "classifications": [
                m.classification.value for m in inbound if m.classification is not None
            ],
            "s4_replies": len(s4_replies),
            "s4_replies_sent": len(s4_sent),
            "s4_replies_auto": len(s4_auto),
            "s4_sensitive": sum(1 for m in s4_replies if (m.meta or {}).get("contains_sensitive")),
            "s4_queued": s4_queued,
            "s4_blocked": sum(1 for m in s4_replies if m.policy_decision is PolicyDecision.BLOCKED),
            "photo_link_sent": bool(intake is not None and intake.photo_link_sent_at is not None),
            "intake_complete": bool(intake is not None and intake.intake_complete),
            "intakes": len(intakes),
            "photos": len(intake.photos or []) if intake is not None else 0,
            "offer_status": offer.status.value if offer is not None else None,
            "arv_confidence": (
                offer.arv_confidence.value if offer is not None and offer.arv_confidence else None
            ),
            "gates_failed": gates_failed,
            "open_flags": sorted(offer.open_flags) if offer is not None else [],
            "condition_class_final": record.get("condition_class_final"),
            "comps_rejected": len(record.get("comps_rejected") or []),
            "comp_tier": offer.comp_tier if offer is not None else None,
            "offer_sent": bool(offer is not None and offer.sent_at is not None),
            "offer_auto": (
                bool((offer_sent_event.payload or {}).get("auto")) if offer_sent_event else None
            ),
            "offer_changed_by_v6": v6_offer_changed,
            "blocked_reason_prefix": (
                str(record.get("blocked_reason") or "")[
                    : len(str(expected.get("blocked_reason_prefix", "")))
                ]
                if record.get("blocked_reason")
                else None
            ),
            "verification_passes": passes,
            "v1_result": (by_pass.get("V1") or [""])[-1] or None,
            "v2_results": by_pass.get("V2", []),
            "v5_results": by_pass.get("V5", []),
            "v6_results": by_pass.get("V6", []),
            "alert_raised": alert_raised,
            "counters": len(counters),
            "counter_within_room": bool(counters[-1].get("within_room")) if counters else None,
            "counter_decision": counters[-1].get("decision") if counters else None,
            "contract_requested": contract_requested,
            "accepted_by": (
                "human"
                if accepted_event is not None and accepted_event.actor is Actor.HUMAN
                else (accepted_event.actor_id if accepted_event is not None else None)
            ),
            "negotiation_outcome": getattr(negotiation_result, "outcome", None),
            "reminders_sent": list(getattr(negotiation_result, "reminders_sent", []) or []),
            "reengagements_sent": list(getattr(negotiation_result, "reengagements_sent", []) or []),
            "reminders_delivered": sum(
                1
                for m in offer_rows
                if (m.meta or {}).get("offer_message_kind") == "reminder" and m.sent_at is not None
            ),
            "reengagements_delivered": sum(
                1
                for m in offer_rows
                if (m.meta or {}).get("offer_message_kind") == "reengage" and m.sent_at is not None
            ),
            "reminders_queued": sum(
                1
                for e in events
                if e.event_type == "offer.reminder_decided"
                and offer is not None
                and e.entity_id == offer.id
                and (e.payload or {}).get("decision") == PolicyDecision.QUEUE_FOR_HUMAN.value
            ),
            "requeued": bool(getattr(negotiation_result, "requeued", False)),
            "contract_status": contract.status.value if contract is not None else None,
            "blast_recipients": len(blast_rows),
            "blast_auto": sum(
                1
                for m in blast_rows
                if m.sent_at is not None and not (m.meta or {}).get("human_approved")
            ),
            "buyer_selected": bool(contract is not None and contract.buyer_investor_id is not None),
            "deposit_recorded": bool(
                contract is not None and contract.deposit_received_at is not None
            ),
            "realized_fee": (
                str(contract.realized_assignment_fee)
                if contract is not None and contract.realized_assignment_fee is not None
                else None
            ),
            "deal_dead_reason": contract.deal_dead_reason if contract is not None else None,
            "earnest_release_reminder": release_reminder,
            "disposition_outcome": getattr(disposition_result, "outcome", None),
            "suppression_reason": suppression.reason if suppression is not None else None,
            "leads_for_address": rows["leads_for_address"],
            "form_lead_created": persona.form_lead_created,
            "s1_rows_merged": (
                int(((lead.source_refs or {}).get("distress") or {}).get("records", 0))
                if isinstance((lead.source_refs or {}).get("distress"), dict)
                else len((lead.source_refs or {}).get("distress") or [])
            ),
        }
        checks = [
            Check(name, exp, actual.get(name), exp == actual.get(name))
            for name, exp in expected.items()
        ]
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
            market=SELLER_MARKET,
            investor=lead.owner_name,
            status=status,
            checks=checks,
            covers=scenario.covers,
            known_defects=sorted(known),
            detail={
                "lead_id": str(lead.id),
                "email": persona.email,
                "sequence_workflow_id": persona.sequence_workflow_id,
                "offer_id": str(offer.id) if offer else None,
                "offer_price": str(offer.offer_price) if offer and offer.offer_price else None,
                "mao": str(offer.mao) if offer and offer.mao else None,
                "arv": str(offer.arv) if offer and offer.arv else None,
                "contract_id": str(contract.id) if contract else None,
                "touches": {str(k): v.isoformat() for k, v in sorted(persona.touches.items())},
                "injected": [i.internet_message_id for i in persona.injected],
                "negotiation_signals": list(persona.negotiation_signals),
                "actual": {k: v for k, v in actual.items() if k not in expected},
            },
        )

    def _grade_cohort(self, scenario: SellerScenario) -> ScenarioResult:
        refused = self.refresh.get(WATCH_MARKET, {})
        s1 = refused.get("s1") or {}
        actual = {
            "refused_reason": s1.get("refused_reason"),
            "leads": int(s1.get("queued", 0)) + int(s1.get("created", 0)),
        }
        expected = scenario.expected(self._level)
        checks = [Check(k, v, actual.get(k), v == actual.get(k)) for k, v in expected.items()]
        status: Status = "pass" if all(c.ok for c in checks) else "fail"
        return ScenarioResult(
            scenario_id=scenario.id,
            title=scenario.title,
            market=WATCH_MARKET,
            investor=None,
            status=status,
            checks=checks,
            covers=scenario.covers,
        )

    async def _aggregates(self, s: AsyncSession) -> tuple[SellerAggregates, list[dict[str, Any]]]:
        a = SellerAggregates()
        markets = (await s.execute(select(Market))).scalars().all()
        a.markets_active = sum(1 for m in markets if m.tier is MarketTier.ACTIVE)
        s1 = (self.refresh.get(SELLER_MARKET) or {}).get("s1") or {}
        a.s1_records_pulled = int(s1.get("records_pulled", 0))
        a.s1_parcels = int(s1.get("parcels", 0))
        a.s1_leads_created = int(s1.get("created", 0))
        a.s1_rejected_by_reason = dict(s1.get("rejected_by_reason") or {})
        for name, result in self.refresh.items():
            reason = (result.get("s1") or {}).get("refused_reason")
            if reason:
                a.s1_refused_markets[name] = str(reason)
        houston = self.refresh.get(SELLER_MARKET) or {}
        a.leads_traced = int(houston.get("traced", 0))
        a.leads_reachable = int(houston.get("reachable", 0))
        a.leads_unreachable = int(houston.get("unreachable", 0))
        leads = (await s.execute(select(SellerLead))).scalars().all()
        a.leads_total = len(leads)
        a.leads_by_source = dict(Counter(str(x.source) for x in leads))
        a.leads_by_status = dict(Counter(x.status.value for x in leads))
        a.leads_dnc_listed = sum(
            1 for x in leads if ((x.source_refs or {}).get("skip_trace") or {}).get("dnc_listed")
        )
        a.seeded_leads = sum(1 for x in leads if x.source == "pilot_seed")
        sequences = (await s.execute(select(SequenceRow))).scalars().all()
        a.sequences_started = len(sequences)
        a.sequences_completed = sum(1 for q in sequences if q.status.value == "completed")
        a.sequences_cancelled = dict(
            Counter(q.cancel_reason or "?" for q in sequences if q.status.value == "cancelled")
        )
        messages = (await s.execute(select(Message))).scalars().all()
        outbound = [m for m in messages if m.direction == Direction.OUT]
        touches = [m for m in outbound if m.sequence_id is not None and m.sent_at is not None]
        a.touches_sent = len(touches)
        a.touches_sent_by_step = dict(Counter(str(m.sequence_step) for m in touches))
        a.touches_auto_sent = sum(1 for m in touches if not (m.meta or {}).get("human_approved"))
        inbound = [m for m in messages if m.direction == Direction.IN]
        a.inbound_injected = sum(len(p.injected) for p in self.personas.values())
        a.inbound_rows = sum(1 for m in inbound if m.channel is Channel.EMAIL)
        a.form_submissions = sum(1 for p in self.personas.values() if p.submission_id)
        a.form_rows = sum(1 for m in inbound if m.channel is Channel.WEB_FORM)
        a.classifications = dict(
            Counter(m.classification.value for m in inbound if m.classification is not None)
        )
        replies = [m for m in outbound if m.policy_action == PolicyAction.SELLER_INTAKE_REPLY.value]
        a.intake_replies = sum(1 for m in replies if not (m.meta or {}).get("supersedes"))
        a.intake_replies_sent = sum(1 for m in replies if m.sent_at is not None)
        a.intake_replies_auto_sent = sum(
            1
            for m in replies
            if m.sent_at is not None
            and (m.meta or {}).get("human_decision_applied") is None
            and (m.meta or {}).get("human_decision") is None
        )
        a.intake_replies_sensitive = sum(
            1 for m in replies if (m.meta or {}).get("contains_sensitive")
        )
        intakes = (await s.execute(select(Intake))).scalars().all()
        a.intakes = len(intakes)
        a.intakes_complete = sum(1 for i in intakes if i.intake_complete)
        a.photo_links_sent = sum(1 for i in intakes if i.photo_link_sent_at is not None)
        offers = (await s.execute(select(Offer))).scalars().all()
        a.offers_computed = len(offers)
        a.offers_by_status = dict(Counter(o.status.value for o in offers))
        a.offers_by_confidence = dict(
            Counter(o.arv_confidence.value for o in offers if o.arv_confidence is not None)
        )
        gate_hist: Counter[str] = Counter()
        for o in offers:
            for g in o.gates:
                if not g.get("passed"):
                    gate_hist[str(g["name"])] += 1
        a.gates_failed = dict(gate_hist)
        a.offers_all_gates_passed = sum(
            1 for o in offers if o.gates and all(g.get("passed") for g in o.gates)
        )
        verifications = (await s.execute(select(OfferVerification))).scalars().all()
        a.verification_results = dict(
            Counter(f"{v.pass_id.value}:{v.result.value}" for v in verifications)
        )
        a.offers_sent = sum(1 for o in offers if o.sent_at is not None)
        events = (await s.execute(select(AuditEvent))).scalars().all()
        a.offers_auto_sent = sum(
            1 for e in events if e.event_type == "offer.sent" and (e.payload or {}).get("auto")
        )
        a.offers_queued = sum(
            1
            for e in events
            if e.event_type == "offer.policy_decided"
            and (e.payload or {}).get("decision") == PolicyDecision.QUEUE_FOR_HUMAN.value
        )
        a.offers_blocked = sum(1 for o in offers if o.status is OfferStatus.BLOCKED)
        counters = [e for e in events if e.event_type == "offer.countered"]
        a.counters = len(counters)
        a.counters_auto_accepted = sum(1 for e in counters if (e.payload or {}).get("accepted"))
        a.counters_to_human = sum(
            1
            for e in counters
            if (e.payload or {}).get("decision") == PolicyDecision.QUEUE_FOR_HUMAN.value
        )
        a.offers_accepted = sum(1 for o in offers if o.status is OfferStatus.ACCEPTED)
        a.offers_declined = sum(1 for o in offers if o.status is OfferStatus.DECLINED)
        a.offers_expired = sum(1 for o in offers if o.status is OfferStatus.EXPIRED)
        reminders = [m for m in outbound if (m.meta or {}).get("offer_message_kind") == "reminder"]
        a.reminders_sent = sum(1 for m in reminders if m.sent_at is not None)
        a.reminders_queued = sum(
            1 for m in reminders if m.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
        )
        a.reengagements_sent = sum(
            1
            for m in outbound
            if (m.meta or {}).get("offer_message_kind") == "reengage" and m.sent_at is not None
        )
        a.requeued_leads = sum(1 for e in events if e.event_type == "seller.requeued")
        a.contracts_requested = sum(1 for e in events if e.event_type == "contract.requested")
        contracts = [
            c
            for c in (await s.execute(select(Contract))).scalars().all()
            if c.type is ContractType.PSA
        ]
        a.contracts_signed = sum(1 for c in contracts if c.signed_at is not None)
        a.contracts_closed = sum(1 for c in contracts if c.status.value == "closed")
        a.contracts_dead = sum(1 for c in contracts if c.status.value == "cancelled")
        blasts = [
            m for m in outbound if m.policy_action == PolicyAction.BUYER_DISPOSITION_BLAST.value
        ]
        a.blasts_drafted = len(blasts)
        a.blasts_sent = sum(1 for m in blasts if m.sent_at is not None)
        a.blasts_auto_sent = sum(
            1 for m in blasts if m.sent_at is not None and not (m.meta or {}).get("human_approved")
        )
        a.buyers_selected = sum(1 for c in contracts if c.buyer_investor_id is not None)
        a.deposits_recorded = sum(1 for c in contracts if c.deposit_received_at is not None)
        a.realized_fees_usd = str(
            sum((c.realized_assignment_fee or Decimal(0) for c in contracts), Decimal(0))
        )
        suppressions = (await s.execute(select(Suppression))).scalars().all()
        a.suppressions = dict(Counter(x.reason for x in suppressions))
        a.queue_depth_end = sum(
            1 for m in outbound if m.draft and m.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
        )
        a.approvals = sum(
            1 for e in events if e.event_type in (EVENT_APPROVED, EVENT_APPROVED_WITH_EDITS)
        )
        a.approval_failures = self._operator.approval_failures
        hist: Counter[str] = Counter()
        queued_total = 0
        for e in events:
            p = e.payload or {}
            if e.event_type in (
                "outreach.policy_decided",
                "offer.policy_decided",
                "disposition.policy_decided",
            ):
                hist[f"{p.get('action')}:{p.get('decision')}"] += 1
                queued_total += int(p.get("decision") == PolicyDecision.QUEUE_FOR_HUMAN.value)
            elif e.event_type == "intake.reply_decided":
                hist[f"{p.get('policy_action')}:{p.get('policy_decision')}"] += 1
                queued_total += int(
                    p.get("policy_decision") == PolicyDecision.QUEUE_FOR_HUMAN.value
                )
            elif e.event_type in ("offer.reminder_decided", "offer.reengage_decided"):
                hist[
                    f"offer_send:{e.event_type.split('.')[1].split('_')[0]}:{p.get('decision')}"
                ] += 1
                queued_total += int(p.get("decision") == PolicyDecision.QUEUE_FOR_HUMAN.value)
            elif e.event_type == "offer.countered":
                hist[f"counter_acceptance:{p.get('decision')}"] += 1
        a.policy_decisions = dict(hist)
        a.queue_items_total = queued_total
        reviews = (await s.execute(select(QaReview))).scalars().all()
        a.qa_verdicts = dict(Counter(r.verdict.value for r in reviews))
        a.qa_flags = dict(Counter(f for r in reviews for f in (r.flags or [])))
        runs = (await s.execute(select(AgentRun))).scalars().all()
        a.agent_runs = len(runs)
        a.cost_usd = str(sum((r.cost_usd for r in runs), Decimal("0")))
        a.model_calls = len(self.provider.requests)
        a.model_calls_by_agent_task = dict(
            Counter(
                f"{r.metadata.agent.value}:{r.metadata.task_kind.value}"
                for r in self.provider.requests
            )
        )
        a.sms_rows = sum(1 for m in messages if m.channel is Channel.SMS)
        a.signal_contract_errors = len(self.sim.signal_contract_errors)
        a.workflows_started = len(self.sim.instances)
        a.workflows_failed = len(self.sim.errors)
        a.workflows_parked_at_end = len(self.sim.parked)
        # POL-04 counts a trailing window over wall-clock ``created_at`` rows; the pilot spans
        # ~93 simulated days from a wall-clock instant, so the window is widened to cover the
        # whole run (the live number would be the last 30 days).
        window_days = int(self.sim.elapsed().total_seconds() // 86_400) + 30
        readiness = [
            r.as_dict()
            for r in await all_promotion_readiness(
                s, now=self.sim.clock + timedelta(hours=1), window_days=window_days
            )
            if r.action in SELLER_ACTIONS
        ]
        return a, readiness

    async def _invariants(self, s: AsyncSession) -> list[Check]:
        checks: list[Check] = []
        offers = (await s.execute(select(Offer))).scalars().all()
        verifications = (await s.execute(select(OfferVerification))).scalars().all()
        by_offer: dict[uuid.UUID, set[str]] = {}
        for v in verifications:
            by_offer.setdefault(v.offer_id, set()).add(v.pass_id.value)
        v5_blocked = {
            o.id for o in offers if str(o.calc_record.get("blocked_reason") or "").startswith("V5")
        }
        missing = [
            str(o.id)
            for o in offers
            if o.id not in v5_blocked
            and (by_offer.get(o.id, set()) & {"V1", "V2", "V3", "V4", "V5"})
            != {"V1", "V2", "V3", "V4", "V5"}
        ]
        checks.append(
            Check(
                "A7-18 every offer has V1..V5 verification rows (V5-blocked: V1..V5)",
                [],
                missing,
                not missing,
            )
        )
        missing_v6 = [
            str(o.id)
            for o in offers
            if o.sent_at is not None and "V6" not in by_offer.get(o.id, set())
        ]
        checks.append(Check("A7-17 every sent offer has a V6 row", [], missing_v6, not missing_v6))
        injected = {
            p.offer_id
            for p in self.personas.values()
            if p.scenario.inject_v5_mismatch and p.offer_id
        }
        v5_fail = [
            str(v.offer_id)
            for v in verifications
            if v.pass_id.value == "V5" and v.result.value != "pass" and v.offer_id not in injected
        ]
        checks.append(
            Check(
                "A7-16 V5 matches the engine unless the pilot injected the bug",
                [],
                v5_fail,
                not v5_fail,
            )
        )
        events = (await s.execute(select(AuditEvent))).scalars().all()
        automated = [
            f"{e.event_type}:{e.actor.value}"
            for e in events
            if e.entity_type == "contract"
            and e.event_type in HUMAN_CONTRACT_EVENTS
            and e.actor is not Actor.HUMAN
        ]
        checks.append(
            Check(
                "POL-03i/j no automated actor touched contracts or earnest money",
                [],
                automated,
                not automated,
            )
        )
        contracts = (await s.execute(select(Contract))).scalars().all()
        auto_signed = [
            str(c.id)
            for c in contracts
            if c.signed_at is not None and c.signed_by in (None, "", "S6", "A7", "S5", "system")
        ]
        checks.append(
            Check(
                "SEL-11 every signed contract names a human signer",
                [],
                auto_signed,
                not auto_signed,
            )
        )
        sender = SenderIdentity.from_settings(self.settings)
        seller_threads = {
            t.id
            for t in (
                await s.execute(select(Thread).where(Thread.seller_lead_id.is_not(None)))
            ).scalars()
        }
        messages = (await s.execute(select(Message))).scalars().all()
        sent_seller = [
            m
            for m in messages
            if m.direction == Direction.OUT
            and m.sent_at is not None
            and m.thread_id in seller_threads
        ]
        lacking_footer = [
            str(m.id)
            for m in sent_seller
            if set(missing_compliance_blocks(m.body, sender)) - {"disclosure"}
        ]
        checks.append(
            Check(
                "SEL-05 every sent seller message carries identity + postal address + opt-out",
                [],
                lacking_footer,
                not lacking_footer,
            )
        )
        lacking_disclosure = [
            str(m.id)
            for m in sent_seller
            if "disclosure" in missing_compliance_blocks(m.body, sender)
        ]
        checks.append(
            Check(
                "SEL-05 / CMP-04 every sent seller message carries the canonical TX disclosure",
                [],
                lacking_disclosure,
                not lacking_disclosure,
                note=(
                    f"known defect {GAP_S12}: offers / reminders / re-engagements carry "
                    "a7.templates.TX_DISCLOSURE, a different wording"
                )
                if lacking_disclosure
                else None,
            )
        )
        sms = sum(1 for m in messages if m.channel is Channel.SMS)
        sms_decisions = sum(
            1
            for e in events
            if (e.payload or {}).get("action") == PolicyAction.SELLER_OUTREACH_SMS.value
        )
        checks.append(
            Check(
                "POL-03e / RUL-06 no SMS row and no SMS policy decision anywhere",
                0,
                sms + sms_decisions,
                sms + sms_decisions == 0,
            )
        )
        injected_emails = sum(len(p.injected) for p in self.personas.values())
        email_rows = sum(
            1 for m in messages if m.direction == Direction.IN and m.channel is Channel.EMAIL
        )
        checks.append(
            Check(
                "INV-24 one inbound row per injected seller email (webhook delivered twice)",
                injected_emails,
                email_rows,
                email_rows == injected_emails,
            )
        )
        forms = sum(1 for p in self.personas.values() if p.submission_id)
        form_rows = sum(1 for m in messages if m.channel is Channel.WEB_FORM)
        checks.append(
            Check("SEL-12 one web_form row per submission", forms, form_rows, forms == form_rows)
        )
        unexpected = [x for x in self._operator.log if "webhook_unexpected" in x]
        checks.append(
            Check("webhook: duplicate notification is a no-op", 0, len(unexpected), not unexpected)
        )
        sent_rows = sum(
            1 for m in messages if m.direction == Direction.OUT and m.sent_at is not None
        )
        checks.append(
            Check(
                "RUL-05 one mock send per sent outbound row",
                sent_rows,
                len(self.graph.sent),
                sent_rows == len(self.graph.sent),
            )
        )
        runs = int((await s.execute(select(func.count()).select_from(AgentRun))).scalar_one())
        checks.append(
            Check(
                "XC-02 one agent_runs row per model call",
                len(self.provider.requests),
                runs,
                runs == len(self.provider.requests),
            )
        )
        leads = (await s.execute(select(SellerLead))).scalars().all()
        dup = [
            k for k, n in Counter(x.address_key for x in leads if x.address_key).items() if n > 1
        ]
        checks.append(Check("SEL-02 one seller lead per normalised address", [], dup, not dup))
        suppressed_sends = 0
        for sup in (await s.execute(select(Suppression))).scalars().all():
            cutoff = self.sim_start
            if sup.source_message_id is not None:
                source = await s.get(Message, sup.source_message_id)
                if source is not None and source.received_at is not None:
                    cutoff = _aware(source.received_at) or cutoff
            for m in messages:
                if m.direction != Direction.OUT or m.sent_at is None:
                    continue
                if (_aware(m.sent_at) or cutoff) <= cutoff:
                    continue
                if any(
                    normalize_email(t) == sup.normalized_value for t in m.to_addresses if "@" in t
                ):
                    suppressed_sends += 1
        checks.append(
            Check(
                "POL-03l no send to a suppressed seller after suppression",
                0,
                suppressed_sends,
                suppressed_sends == 0,
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

    def _gaps(self, a: SellerAggregates) -> list[Gap]:
        gaps: list[Gap] = [
            Gap(
                id="GAP-S01",
                severity="low",
                where="integrations/data/fixtures/{distress,skiptrace}.json",
                behaviour=(
                    "The Houston distress fixture yields four reachable owners (Ramirez, Nguyen, "
                    "Pike, "
                    "Okafor); the seller pilot needs seven sequence personas, so three leads are "
                    "entered by hand (source=pilot_seed) and started through the production "
                    "starter."
                ),
                expected=(
                    "Enough fixture rows for a no-reply, an opt-out and a price-asker persona."
                ),
                evidence=f"seeded leads {a.seeded_leads}; S1 created {a.s1_leads_created}.",
            ),
            Gap(
                id="GAP-S02",
                severity="medium",
                where=(
                    "integrations/data/fixtures/properties.json vs distress.json / deeds.json "
                    "(mock.MockDeedRecordsProvider.resolve_parcel, "
                    "property.MockPropertyDataProvider)"
                ),
                behaviour=(
                    "The seven A7 fixture subjects are unknown to the deeds mock, so a web form on "
                    "them never parcel-verifies (S4 keeps asking for the address, no offer); and "
                    "no "
                    "S1 distress address has a property fixture, so A7 blocks every S1 lead with "
                    "'property could not be resolved'. The pilot registers the fixture parcels and "
                    "aliases fixture subjects under the leads' addresses."
                ),
                expected=(
                    "One coherent mock world: distress parcels resolvable by A7, fixture subjects "
                    "resolvable by S4."
                ),
                evidence=(
                    f"offers computed {a.offers_computed}, intakes complete {a.intakes_complete}."
                ),
            ),
            Gap(
                id=GAP_S03,
                severity="medium",
                where=(
                    "api/routes/sell.py (TEXT_FIELDS) + activities/seller.py seller_intake + "
                    "a7/gates.py"
                ),
                behaviour=(
                    "The web form has no co-owner / liens / HOA question and S4 asks optional "
                    "fields "
                    "only while a required one is missing, so a complete form intake reaches A7 "
                    "with "
                    "co_owners_unknown=True -> open flag co_owner_missing -> gate no_open_flags "
                    "fails -> every form-sourced offer is 'escalate' and queues for a human even "
                    "at L1."
                ),
                expected=(
                    "Either the form asks the SEL-06 optional questions or S4 asks them once "
                    "before "
                    "completing; a clean form intake should pass the gates."
                ),
                evidence=f"gates failed {a.gates_failed}; offers auto-sent {a.offers_auto_sent}.",
            ),
            Gap(
                id=GAP_S04,
                severity="high",
                where=(
                    "activities/seller.py seller_classify_inbound / seller_intake vs "
                    "workflows/negotiation.py"
                ),
                behaviour=(
                    "A seller's reply after the offer (accept, counter, decline) runs through "
                    "SellerInboundWorkflow as an intake message: the intake is already complete, "
                    "so "
                    "nothing is drafted and NegotiationWorkflow never hears of it. No code path "
                    "signals counter/accept/decline from inbound mail; the pilot signals S5 itself."
                ),
                expected=(
                    "Post-offer seller replies are classified (accept / counter with amount / "
                    "decline / question) and signalled to negotiation:{offer_id} (SEL-09)."
                ),
                evidence=(
                    f"counters {a.counters}, accepted {a.offers_accepted}: all via pilot signals."
                ),
            ),
            Gap(
                id=GAP_S05,
                severity="high",
                where=(
                    "workflows/negotiation.py (no human_decision signal) vs activities/offers.py "
                    "send_negotiation_message"
                ),
                behaviour=(
                    "At L0 the S5 reminders (days 2/5/10) and re-engagements queue for a human "
                    "with "
                    "meta.temporal_workflow_id=negotiation:{offer_id}; the review route resolves "
                    "that "
                    "id and signals human_decision, which NegotiationWorkflow does not define -> "
                    "502, "
                    "so no reminder can ever be approved and the queue fills with unresolvable "
                    "items."
                ),
                expected=(
                    "NegotiationWorkflow accepts human_decision (send via a send_approved "
                    "activity) "
                    "or the reminders are sent by a child workflow that does."
                ),
                evidence=(
                    f"reminders queued {a.reminders_queued}, sent {a.reminders_sent}; approval "
                    f"failures {a.approval_failures}."
                ),
            ),
            Gap(
                id="GAP-S06",
                severity="medium",
                where="api/routes/deals.py mark_signed",
                behaviour=(
                    "The route starts DispositionWorkflow before committing the signed status; "
                    "with "
                    "a real Temporal server the worker's load_contract can read the row as "
                    "sent_for_signature and finish 'not_signed' (no blast, no alerts). Not "
                    "evidenced "
                    "on SQLite (shared connection; the pilot gateway dispatches at the next turn)."
                ),
                expected="Commit the human's write first, then start the workflow (as /sell does).",
                evidence="by code inspection",
            ),
            Gap(
                id="GAP-S07",
                severity="low",
                where="agents/a7/engine.py run_v5 / compute_mao",
                behaviour=(
                    "The engine exposes no hook to exercise a V5 mismatch; the pilot swaps "
                    "engine.compute_mao for one computation, like tests/agents/a7/test_engine.py."
                ),
                expected="An injection seam (e.g. an optional recompute_override on the engine).",
                evidence=f"V5 results {a.verification_results.get('V5:fail', 0)} fail row(s).",
            ),
        ]
        gaps.extend(
            [
                Gap(
                    id=GAP_S08,
                    severity="high",
                    where=(
                        "agents/a5.py run_prechecks (cadence_violation) <- activities/seller.py "
                        "seller_intake"
                    ),
                    behaviour=(
                        "A seller who answers within 24 h of our note gets no reply: A5's "
                        "cadence pre-check flags the S4 intake reply because *our* last outbound "
                        "on the thread is < 24 h old, the policy engine turns the BLOCK verdict "
                        "into a blocked draft, the inbound workflow ends and nothing retries "
                        "later; the sequence was already cancelled on `reply`, so the lead goes "
                        "dark. The same rule applies to A4 investor replies (the investor pilot "
                        "avoided it with >= 26 h scripted delays)."
                    ),
                    expected=(
                        "An answer to an inbound message is not a cadence violation (skip the "
                        "check when the draft replies to a newer inbound), or defer the send "
                        "until the gap has passed instead of blocking."
                    ),
                    evidence=(
                        "intake replies blocked "
                        f"{a.policy_decisions.get('seller_intake_reply:blocked', 0)}; "
                        f"QA flags {a.qa_flags}."
                    ),
                ),
                Gap(
                    id=GAP_S09,
                    severity="medium",
                    where="agents/a7/gates.py derive_open_flags / _NEGATIVE",
                    behaviour=(
                        "co_owners text such as 'sole owner' or 'just me' is read as co-owners "
                        "who are not identified (only no/none/n/a count as negative), so the "
                        "co_owner_missing flag fires and gate no_open_flags fails for a seller "
                        "who said they own the house alone."
                    ),
                    expected=(
                        "Sole-ownership statements clear the flag (or S4 normalises them to "
                        "'none')."
                    ),
                    evidence=f"gates failed {a.gates_failed}.",
                ),
                Gap(
                    id="GAP-S10",
                    severity="low",
                    where="api/routes/sell.py (datetime.now) and api/routes/deals.py _now()",
                    behaviour=(
                        "The web form stamps received_at / submitted_at and the /deals routes "
                        "stamp buyer responses and defaults with the wall clock, not an "
                        "injectable clock, so the form row sits out of order on the simulated "
                        "timeline and the pilot has to pass every date explicitly."
                    ),
                    expected=(
                        "Routes read the clock the activities read (app.state.clock or a dep)."
                    ),
                    evidence="form rows carry the wall-clock time of the run",
                ),
                Gap(
                    id=GAP_S11,
                    severity="low",
                    where="db/repositories/contracts.py mark_signed / mark_closed / mark_dead",
                    behaviour=(
                        "Nothing moves the seller lead past `offered`: the signed contract, the "
                        "closing and the dead deal leave seller_leads.status unchanged although "
                        "the enum has under_contract / closed / dead and handoff_to_s5 and "
                        "requeue_seller guard on them."
                    ),
                    expected=(
                        "mark_signed -> under_contract, mark_closed -> closed, mark_dead -> dead."
                    ),
                    evidence=f"leads by status {a.leads_by_status}.",
                ),
            ]
        )
        gaps.append(
            Gap(
                id=GAP_S12,
                severity="medium",
                where="agents/a7/templates.py TX_DISCLOSURE vs agents/templates/seller_outreach.py",
                behaviour=(
                    "The offer, reminder and re-engagement emails append A7's own TX_DISCLOSURE "
                    "text, not TX_EQUITABLE_INTEREST_DISCLOSURE (the SEL-05 block counsel reviews "
                    "under PRE-16 and missing_compliance_blocks checks), so two different "
                    "disclosures reach sellers from one mailbox."
                ),
                expected="One disclosure constant, rendered by seller_outreach.compliance_block.",
                evidence="every sent offer / reminder / re-engagement fails the canonical check",
            )
        )
        if a.signal_contract_errors:
            gaps.insert(
                0,
                Gap(
                    id="GAP-01",
                    severity="high",
                    where="signal payloads vs handlers (regression of the investor pilot's GAP-01)",
                    behaviour="A signal carried a payload the handler could not accept.",
                    expected="Zero signal contract errors.",
                    evidence=f"{a.signal_contract_errors} contract errors in this run.",
                ),
            )
        return gaps

    def _notes(self) -> list[str]:
        notes = [
            "Mock model, mock Graph, mock property / distress / skip-trace / DNC data, mock "
            "DocuSign, simulated Temporal: nothing real was touched.",
            f"M1: {len(self.m1_report.get('rows', []))} markets scored; the operator forced "
            f"{SELLER_MARKET} to Active (PRE-17), {WATCH_MARKET} stays Watch.",
            f"A1 {SELLER_MARKET}: qualified {self.a1_result.get('qualified')}; {len(self.buyers)} "
            "investors given an operator-entered buy box as S6 buyers.",
            f"S1/S2 {SELLER_MARKET}: {self.refresh.get(SELLER_MARKET, {}).get('s1', {})}",
            f"operator: {self._operator.approvals} approvals through POST /review/{{id}}/approve, "
            f"{self._operator.approval_failures} failed; {len(self._operator.deals)} /deals "
            f"clicks; "
            f"delay {self.operator_delay}",
            f"model script hits: {dict(self.scripts.hits)}",
        ]
        if self.unbound:
            notes.extend(f"unbound scenario {k}: {v}" for k, v in self.unbound.items())
        return notes

    async def _record_report(self, report: SellerPilotReport) -> None:
        assert self.session_factory is not None
        async with self.session_factory() as s:
            await AuditRepository(s).record(
                actor=Actor.SYSTEM,
                actor_id="pilot",
                event_type="seller_pilot.completed",
                entity_type="pilot",
                payload=report.to_dict(),
            )
            await s.commit()


FIXTURE_SUBJECTS: tuple[tuple[str, str], ...] = (
    ("1204 Oak Hollow Dr", "77008"),
    ("88 Pine Ridge Ln", "77018"),
    ("4510 Maple Ct", "77092"),
    ("61 Elm Loop", "77022"),
    ("930 Cedar Bend", "77055"),
    ("2201 Birch St", "77007"),
    ("717 Willow Way", "77009"),
)


class _ApiSession:
    """The FastAPI app over the pilot's engine, gateway and mock blob store, as an httpx client."""

    def __init__(self, runner: SellerPilotRunner) -> None:
        self._runner = runner
        self._app: FastAPI | None = None
        self._client: AsyncClient | None = None
        self._lifespan: Any = None

    async def __aenter__(self) -> AsyncClient:
        runner = self._runner
        app = create_app(runner.settings)
        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()
        await app.state.engine.dispose()
        app.state.engine = runner.engine
        app.state.session_factory = runner.session_factory
        app.state.temporal = runner.gateway
        app.state.blob = MockBlobStorage()
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
            self._app.state.engine = make_engine("sqlite+aiosqlite:///:memory:")
        if self._lifespan is not None:
            await self._lifespan.__aexit__(None, None, None)


__all__ = [
    "AS_OF",
    "DEFAULT_SIM_START",
    "SELLER_ACTIONS",
    "SIM_HORIZON",
    "InjectedEmail",
    "SellerPersona",
    "SellerPilotRunner",
    "SellerScriptBook",
    "alias_subject",
    "buggy_compute_mao",
    "drift_comps",
    "register_parcel",
]
