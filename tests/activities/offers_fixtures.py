"""Context wiring and row factories shared by the offer / negotiation / deals activity tests and
the Temporal end-to-end tests: mock Graph, mock model (A5 approves, A7 audit flags nothing beyond
the pre-filters), mock Temporal, fixture property provider, SQLite."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wholesaling.activities import conversation as conv
from wholesaling.activities import graph as gacts
from wholesaling.activities import offers as oacts
from wholesaling.agents.a4 import MODEL_OVERRIDES as A4_OVERRIDES
from wholesaling.agents.a7.audit import MODEL_OVERRIDES as A7_OVERRIDES
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.config.tunables import get_tunables
from wholesaling.db.models.enums import AgentId, ConditionClass, IntakeTimeline, MarketTier
from wholesaling.db.models.markets import Market
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    MockPropertyDataProvider,
    mock_property_provider,
)
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.policy.actions import AutonomyLevel, PolicyAction

MAILBOX = "deals@example.test"
SELLER_EMAIL = "pat.doe@example.test"
NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
assert NOW.date() == DEFAULT_TODAY


@dataclass
class FakeClock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def offer_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "graph_mailbox": MAILBOX,
        "sender_postal_address": "123 Main St, Houston, TX 77002",
    }
    base.update(overrides)
    return Settings(**base)


@dataclass
class OfferHarness:
    graph: MockGraphGateway
    model: MockModelProvider
    temporal: MockTemporalGateway
    property_provider: MockPropertyDataProvider
    clock: FakeClock
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    runner: AgentRunner = field(init=False)

    def __post_init__(self) -> None:
        self.runner = AgentRunner(
            self.model,
            ModelRouter.from_settings(self.settings, {**A4_OVERRIDES, **A7_OVERRIDES}),
        )

    def install(self) -> None:
        gacts.set_graph_context(
            gacts.GraphActivityContext(
                gateway=self.graph,
                session_factory=self.session_factory,
                settings=self.settings,
                clock=self.clock,
            )
        )
        conv.set_conversation_context(
            conv.ConversationContext(
                session_factory=self.session_factory,
                runner=self.runner,
                temporal=self.temporal,
                settings=self.settings,
                clock=self.clock,
            )
        )
        oacts.set_offer_context(
            oacts.OfferActivityContext(
                property_provider=self.property_provider,
                runner=self.runner,
                tunables=get_tunables(),
            )
        )

    @staticmethod
    def uninstall() -> None:
        oacts.set_offer_context(None)
        conv.set_conversation_context(None)
        gacts.set_graph_context(None)


def qa_approve(confidence: float = 0.95) -> dict[str, object]:
    return {
        "verdict": "approve",
        "confidence": confidence,
        "flags": [],
        "rewrite_body": None,
        "rationale": "scripted",
    }


@pytest.fixture
def offer_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def offer_harness_settings() -> Settings:
    return offer_settings()


@pytest.fixture
async def offer_harness(
    session_factory: async_sessionmaker[AsyncSession],
    offer_clock: FakeClock,
    offer_harness_settings: Settings,
) -> AsyncIterator[OfferHarness]:
    clock = offer_clock
    harness_settings = offer_harness_settings
    model = MockModelProvider()
    model.script(AgentId.A5, TaskKind.QA, parsed=qa_approve())
    h = OfferHarness(
        graph=MockGraphGateway(mailbox=MAILBOX, clock=clock),
        model=model,
        temporal=MockTemporalGateway(),
        property_provider=mock_property_provider(),
        clock=clock,
        settings=harness_settings,
        session_factory=session_factory,
    )
    h.install()
    try:
        yield h
    finally:
        h.uninstall()


async def seed_market(session: AsyncSession, *, hard_cap: Decimal | None = None) -> Market:
    existing = (
        await session.execute(select(Market).where(Market.name == "Houston", Market.state == "TX"))
    ).scalar_one_or_none()
    if existing is not None:
        existing.deal_band_max = hard_cap
        await session.flush()
        return existing
    market = Market(name="Houston", state="TX", tier=MarketTier.ACTIVE, deal_band_max=hard_cap)
    session.add(market)
    await session.flush()
    return market


async def seed_seller(
    session: AsyncSession,
    *,
    market: Market | None,
    line1: str = "1204 Oak Hollow Dr",
    postal_code: str = "77008",
    email: str | None = SELLER_EMAIL,
    owner_name: str = "Pat Doe",
    condition: ConditionClass = ConditionClass.MEDIUM,
    disclosed_issues: list[str] | None = None,
    asking_price: Decimal | None = Decimal(100000),
    photos: int = 2,
    **intake_overrides: Any,
) -> tuple[SellerLead, Intake]:
    lead = SellerLead(
        market_id=market.id if market else None,
        owner_name=owner_name,
        property_address_line1=line1,
        property_city="Houston",
        property_state="TX",
        property_postal_code=postal_code,
        contact_channels=(
            [{"channel": "email", "value": email, "is_primary": True, "source": "test"}]
            if email
            else []
        ),
        source="test",
    )
    session.add(lead)
    await session.flush()
    fields: dict[str, Any] = {
        "seller_lead_id": lead.id,
        "motivation": "relocating for work",
        "condition_class": condition,
        "disclosed_issues": disclosed_issues if disclosed_issues is not None else ["roof"],
        "timeline": IntakeTimeline.WITHIN_30_DAYS,
        "asking_price": asking_price,
        "photos": [{"blob_key": f"p{i}"} for i in range(photos)],
        "co_owners": "none",
        "co_owners_unknown": False,
        "occupancy": "owner",
        "occupancy_unknown": False,
        "liens": "none",
        "liens_unknown": False,
        "hoa": "no",
        "hoa_unknown": False,
        "intake_complete": True,
        "completed_at": NOW,
    }
    fields.update(intake_overrides)
    intake = Intake(**fields)
    session.add(intake)
    await session.flush()
    return lead, intake


async def set_level(session: AsyncSession, action: PolicyAction, level: AutonomyLevel) -> None:
    await PolicyRepository(session).set_level(action, level, updated_by="test")
    await session.commit()


def new_key() -> str:
    return f"offer:{uuid.uuid4()}:compute"


__all__ = [
    "MAILBOX",
    "NOW",
    "SELLER_EMAIL",
    "FakeClock",
    "OfferHarness",
    "new_key",
    "offer_clock",
    "offer_harness",
    "offer_harness_settings",
    "offer_settings",
    "qa_approve",
    "seed_market",
    "seed_seller",
    "set_level",
]
