"""Fixtures for the operator surface tests (/review, /policies, /digest).

The app is built with SQLite in memory (StaticPool, so every connection sees the same DB), the
mock Temporal gateway and a known operator password. Seeding helpers write ``messages`` +
``qa_reviews`` rows directly, exactly as the A3/A4/A5 builders' activities will.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, BasicAuth
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wholesaling.api.auth import hash_password
from wholesaling.api.main import create_app
from wholesaling.config.settings import Settings
from wholesaling.db import Base, make_session_factory
from wholesaling.db.models.enums import (
    Actor,
    Channel,
    Direction,
    InvestorType,
    MarketTier,
    PolicyDecision,
    QaVerdict,
    SequenceKind,
)
from wholesaling.db.models.investors import Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Sequence, Thread
from wholesaling.db.models.ops import AuditEvent, QaReview
from wholesaling.integrations.temporal.mock import MockTemporalGateway

OPERATOR = "aaron"
PASSWORD = "correct horse battery staple"
# Low iteration count keeps the suite fast; production uses DEFAULT_ITERATIONS.
PASSWORD_HASH = hash_password(PASSWORD, iterations=1_000, salt=b"0123456789abcdef")
AUTH = BasicAuth(OPERATOR, PASSWORD)


def operator_settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "temporal_mode": "mock",
        "graph_mode": "mock",
        "operator_username": OPERATOR,
        "operator_password_hash": PASSWORD_HASH,
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    import wholesaling.db.models  # noqa: F401 - register every table

    application = create_app(operator_settings())
    async with application.router.lifespan_context(application):
        async with application.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def auth_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", auth=AUTH
    ) as c:
        yield c


@pytest.fixture
def temporal(app: FastAPI) -> MockTemporalGateway:
    gw = app.state.temporal
    assert isinstance(gw, MockTemporalGateway)
    return gw


@pytest.fixture
def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(app.state.engine)


# --------------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Seeded:
    market: Market
    investor: Investor
    thread: Thread
    sequence: Sequence | None
    qa: QaReview | None
    message: Message


async def seed_market(session: AsyncSession, name: str = "Houston", state: str = "TX") -> Market:
    stmt = select(Market).where(Market.name == name, Market.state == state)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    market = Market(name=name, state=state, tier=MarketTier.ACTIVE)
    session.add(market)
    await session.flush()
    return market


async def seed_queued_message(
    session: AsyncSession,
    *,
    action: str = "investor_initial_outreach",
    market: Market | None = None,
    investor_name: str = "Lone Star Holdings LLC",
    recipient: str = "buyer@example.com",
    subject: str = "Off-market deals in Houston",
    body: str = "Hi, we source off-market single-family deals in Harris County...",
    with_sequence: bool = True,
    step: int | None = 0,
    workflow_id: str | None = None,
    qa_verdict: QaVerdict | None = QaVerdict.APPROVE,
    qa_confidence: Decimal | None = Decimal("0.91"),
    qa_flags: list[str] | None = None,
    qa_rationale: str | None = "Accurate, compliant, matches the thread.",
    policy_reason: str | None = "draft at L0: human sends",
    created_at: datetime | None = None,
    policy_decision: PolicyDecision = PolicyDecision.QUEUE_FOR_HUMAN,
    draft: bool = True,
    meta: dict[str, Any] | None = None,
    inbound_workflow_audit: str | None = None,
) -> Seeded:
    """Insert market -> investor -> thread -> (sequence) -> message (+ qa_review)."""
    if market is None:
        market = await seed_market(session)
    investor = Investor(
        canonical_name=investor_name,
        entity_key=investor_name.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:6],
        investor_type=InvestorType.FLIPPER,
        market_id=market.id,
    )
    session.add(investor)
    await session.flush()
    thread = Thread(
        channel=Channel.EMAIL,
        external_thread_id=f"conv-{uuid.uuid4().hex}",
        subject=subject,
        investor_id=investor.id,
        counterpart_address=recipient,
    )
    session.add(thread)
    await session.flush()
    sequence: Sequence | None = None
    if with_sequence:
        sequence = Sequence(
            kind=SequenceKind.INVESTOR,
            investor_id=investor.id,
            thread_id=thread.id,
            temporal_workflow_id=workflow_id or f"investor-sequence-{uuid.uuid4().hex[:8]}",
        )
        session.add(sequence)
        await session.flush()
    message = Message(
        thread_id=thread.id,
        direction=Direction.OUT,
        channel=Channel.EMAIL,
        external_message_id=f"draft-{uuid.uuid4().hex}",
        to_addresses=[recipient],
        subject=subject,
        body=body,
        draft=draft,
        policy_action=action,
        policy_decision=policy_decision,
        sequence_id=sequence.id if sequence else None,
        sequence_step=step if sequence else None,
        meta={**({"policy_reason": policy_reason} if policy_reason else {}), **(meta or {})},
    )
    if created_at is not None:
        message.created_at = created_at
    session.add(message)
    await session.flush()
    qa: QaReview | None = None
    if qa_verdict is not None:
        qa = QaReview(
            message_id=message.id,
            verdict=qa_verdict,
            confidence=qa_confidence,
            flags=list(qa_flags or []),
            rationale=qa_rationale,
            model="mock",
        )
        session.add(qa)
        await session.flush()
        message.qa_review_id = qa.id
    if inbound_workflow_audit is not None:
        session.add(
            AuditEvent(
                actor=Actor.SYSTEM,
                event_type="inbound.workflow_started",
                entity_type="message",
                entity_id=message.id,
                payload={"workflow_id": inbound_workflow_audit},
            )
        )
    await session.flush()
    return Seeded(
        market=market, investor=investor, thread=thread, sequence=sequence, qa=qa, message=message
    )


def hours_ago(hours: float) -> datetime:
    return datetime.now(tz=UTC) - timedelta(hours=hours)
