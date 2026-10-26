"""WF-02 sequence starter on SQLite with the mock Temporal gateway (REQUIREMENTS.md INV-04,
INV-08, WF-02, POL-03l; pilot GAP-03 / GAP-07).

Eligibility and ordering, the daily kickoff cap, the deterministic workflow id, the inline start
from ``run_a2``, the ``investor.unreachable`` event, the kickoff activity and the workflow /
schedule / worker registration.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity, workflow

from tests.db.conftest import MakeInvestor
from wholesaling.activities import discovery
from wholesaling.activities import sequence_starter as starter
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, Contact, Investor, Market, Suppression
from wholesaling.db.models.enums import (
    Channel,
    ContactChannel,
    MarketTier,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.messaging import Sequence
from wholesaling.db.normalize import normalize_email
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.data.mock import mock_data_providers
from wholesaling.integrations.temporal import MockTemporalGateway
from wholesaling.reporting.kpis import qualified_unreachable
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows.investor_sequence import (
    WORKFLOW_NAME,
    InvestorSequenceInput,
    investor_sequence_workflow_id,
)
from wholesaling.workflows.outreach_kickoff import OutreachKickoffWorkflow
from wholesaling.workflows.schedules import SCHEDULES, get_schedule

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "app_env": "test", "temporal_mode": "mock"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def temporal() -> MockTemporalGateway:
    return MockTemporalGateway()


@pytest.fixture
async def ctx(
    session_factory: async_sessionmaker[AsyncSession],
    temporal: MockTemporalGateway,
    clock: FakeClock,
) -> AsyncIterator[starter.StarterContext]:
    context = starter.StarterContext(
        session_factory=session_factory, temporal=temporal, settings=_settings(), clock=clock
    )
    starter.set_starter_context(context)
    try:
        yield context
    finally:
        starter.set_starter_context(None)


@pytest.fixture
async def market(session: AsyncSession) -> Market:
    row = Market(name="Houston", state="TX", tier=MarketTier.WATCH)
    session.add(row)
    await session.flush()
    await session.commit()
    return row


async def _investor(
    session: AsyncSession,
    make_investor: MakeInvestor,
    market: Market,
    name: str,
    *,
    score: int | None,
    email: str | None,
    excluded_reason: str | None = None,
) -> Investor:
    inv = await make_investor(
        name, market_id=market.id, score=score, excluded_reason=excluded_reason
    )
    if email is not None:
        session.add(
            Contact(
                investor_id=inv.id,
                channel=ContactChannel.EMAIL,
                value=email,
                normalized_value=normalize_email(email),
                is_primary=True,
            )
        )
    await session.commit()
    return inv


async def _sequences(session: AsyncSession) -> list[Sequence]:
    return list((await session.execute(select(Sequence).order_by(Sequence.created_at))).scalars())


# -- rules ----------------------------------------------------------------------------------------


async def test_candidates_are_priority_first_and_skips_are_explained(
    ctx: starter.StarterContext,
    session: AsyncSession,
    market: Market,
    make_investor: MakeInvestor,
) -> None:
    await _investor(session, make_investor, market, "Prospect LLC", score=60, email="p@x.test")
    await _investor(session, make_investor, market, "Priority LLC", score=88, email="a@x.test")
    await _investor(session, make_investor, market, "Also Priority LLC", score=80, email="b@x.test")
    await _investor(session, make_investor, market, "No Email LLC", score=76, email=None)
    await _investor(session, make_investor, market, "Low Score LLC", score=40, email="low@x.test")
    await _investor(
        session,
        make_investor,
        market,
        "Excluded LLC",
        score=70,
        email="ex@x.test",
        excluded_reason="institutional",
    )
    gone = await _investor(
        session, make_investor, market, "Opted Out LLC", score=70, email="o@x.test"
    )
    session.add(
        Suppression(
            channel=Channel.EMAIL, normalized_value=normalize_email("o@x.test"), reason="opt_out"
        )
    )
    running = await _investor(
        session, make_investor, market, "Running LLC", score=79, email="r@x.test"
    )
    await SequenceRepository(session).create(
        kind=SequenceKind.INVESTOR,
        investor_id=running.id,
        temporal_workflow_id="investor-sequence:x",
    )
    await session.commit()

    candidates, skipped = await starter.list_startable_investors(session, market.id)
    assert [c.investor_name for c in candidates] == [
        "Priority LLC",
        "Also Priority LLC",
        "Prospect LLC",
    ]
    assert [c.priority for c in candidates] == [True, True, False]
    assert skipped == {"no_email": 1, "suppressed": 1, "has_sequence": 1}
    assert gone.id not in {c.investor_id for c in candidates}
    limited, skipped_limited = await starter.list_startable_investors(session, market.id, limit=2)
    assert [c.investor_name for c in limited] == ["Priority LLC", "Also Priority LLC"]
    assert skipped_limited["limit"] == 1


async def test_excluded_market_tier_yields_no_candidates(
    ctx: starter.StarterContext, session: AsyncSession, make_investor: MakeInvestor
) -> None:
    market = Market(name="Nowhere", state="ZZ", tier=MarketTier.EXCLUDED)
    session.add(market)
    await session.flush()
    inv = await _investor(session, make_investor, market, "Fine LLC", score=90, email="f@x.test")
    assert await starter.skip_reason(session, inv, market) == "market_tier"
    result = await starter.start_investor_sequences(
        starter.StartSequencesInput(market_id=str(market.id))
    )
    assert result.started == [] and result.skipped == {"market_tier": 1}


# -- starting -------------------------------------------------------------------------------------


async def test_start_creates_row_with_deterministic_workflow_id_and_starts_workflow(
    ctx: starter.StarterContext,
    session: AsyncSession,
    market: Market,
    make_investor: MakeInvestor,
    temporal: MockTemporalGateway,
) -> None:
    inv = await _investor(session, make_investor, market, "Acme LLC", score=88, email="a@x.test")
    result = await starter.start_investor_sequences(
        starter.StartSequencesInput(market_id=str(market.id))
    )
    assert result.started_count == 1 and result.daily_cap == 50 and result.started_today_before == 0
    started = result.started[0]
    rows = await _sequences(session)
    assert len(rows) == 1
    row = rows[0]
    assert str(row.id) == started["sequence_id"] and row.investor_id == inv.id
    assert (
        row.temporal_workflow_id
        == investor_sequence_workflow_id(str(row.id))
        == started["workflow_id"]
    )
    assert row.status is SequenceStatus.ACTIVE and row.total_steps == 4
    call = next(c for c in temporal.calls if c.method == "start_workflow")
    assert call.args["workflow"] == WORKFLOW_NAME
    assert call.args["workflow_id"] == row.temporal_workflow_id
    (arg,) = call.args["args"]
    assert isinstance(arg, InvestorSequenceInput)
    assert arg == InvestorSequenceInput(
        sequence_id=str(row.id),
        investor_id=str(inv.id),
        contact_email="a@x.test",
        market="Houston",
        intervals_days=[4, 4, 4],
    )
    events = (
        (
            await session.execute(
                select(AuditEvent).where(AuditEvent.event_type == "sequence.started")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1 and events[0].payload["priority"] is True
    assert events[0].payload["workflow_id"] == row.temporal_workflow_id
    # Running it again starts nothing: the investor is on a sequence now.
    again = await starter.start_investor_sequences(
        starter.StartSequencesInput(market_id=str(market.id))
    )
    assert again.started == [] and again.skipped == {"has_sequence": 1}
    assert len(await _sequences(session)) == 1


async def test_daily_cap_from_settings_bounds_the_starts(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    market: Market,
    make_investor: MakeInvestor,
    temporal: MockTemporalGateway,
    clock: FakeClock,
) -> None:
    context = starter.StarterContext(
        session_factory=session_factory,
        temporal=temporal,
        settings=_settings(outreach_kickoff_daily_cap=2),
        clock=clock,
    )
    starter.set_starter_context(context)
    try:
        for i, score in enumerate((90, 80, 70)):
            await _investor(
                session, make_investor, market, f"Inv {i} LLC", score=score, email=f"{i}@x.test"
            )
        first = await starter.start_investor_sequences(
            starter.StartSequencesInput(market_id=str(market.id))
        )
        assert [s["priority"] for s in first.started] == [True, True]  # priority tier first
        assert first.skipped == {"daily_cap": 1} and first.daily_cap == 2
        # Same day: nothing more; next day the third one starts.
        same_day = await starter.start_investor_sequences(
            starter.StartSequencesInput(market_id=str(market.id))
        )
        assert same_day.started == [] and same_day.started_today_before == 2
        clock.now = datetime(2026, 10, 28, 1, 0, tzinfo=UTC)
        next_day = await starter.start_investor_sequences(
            starter.StartSequencesInput(market_id=str(market.id))
        )
        assert next_day.started_count == 1 and next_day.started[0]["priority"] is False
    finally:
        starter.set_starter_context(None)


def test_daily_cap_prefers_caps_yaml_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from wholesaling.config import tunables

    assert tunables.get_tunables().caps.outreach_kickoff_daily_cap is None  # not in caps.yaml
    assert starter.daily_kickoff_cap(_settings(outreach_kickoff_daily_cap=7)) == 7
    patched = tunables.get_tunables().model_copy(deep=True)
    patched.caps.outreach_kickoff_daily_cap = 3
    monkeypatch.setattr(starter, "get_tunables", lambda: patched)
    assert starter.daily_kickoff_cap(_settings(outreach_kickoff_daily_cap=7)) == 3


async def test_failed_workflow_start_cancels_the_row_and_is_retried_later(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    market: Market,
    make_investor: MakeInvestor,
    clock: FakeClock,
) -> None:
    class Flaky(MockTemporalGateway):
        fail = True

        async def start_workflow(self, *a: object, **kw: object) -> object:
            if self.fail:
                msg = "temporal down"
                raise ConnectionError(msg)
            return await super().start_workflow(*a, **kw)  # type: ignore[arg-type]

    gw = Flaky()
    context = starter.StarterContext(
        session_factory=session_factory, temporal=gw, settings=_settings(), clock=clock
    )
    starter.set_starter_context(context)
    try:
        await _investor(session, make_investor, market, "Acme LLC", score=88, email="a@x.test")
        with pytest.raises(ConnectionError):
            await starter.start_investor_sequences(
                starter.StartSequencesInput(market_id=str(market.id))
            )
        rows = await _sequences(session)
        assert len(rows) == 1 and rows[0].status is SequenceStatus.CANCELLED
        assert rows[0].cancel_reason == starter.START_FAILED_REASON
        gw.fail = False
        retry = await starter.start_investor_sequences(
            starter.StartSequencesInput(market_id=str(market.id))
        )
        assert retry.started_count == 1
        rows = await _sequences(session)
        assert [r.status for r in rows] == [SequenceStatus.CANCELLED, SequenceStatus.ACTIVE]
    finally:
        starter.set_starter_context(None)


# -- run_a2 integration (inline start + unreachable) ----------------------------------------------


async def test_run_a2_starts_the_sequence_for_a_qualified_investor_with_an_email(
    ctx: starter.StarterContext,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    market: Market,
    make_investor: MakeInvestor,
    temporal: MockTemporalGateway,
) -> None:
    inv = await make_investor("Lone Star Flips LLC", market_id=market.id, score=75)
    await session.commit()
    discovery.configure(session_factory, mock_data_providers())
    try:
        payload = await discovery.run_a2(str(inv.id))
        assert payload["primary_email"] == "acquisitions@lonestarflips.com"
        assert payload["unreachable"] is False and payload["unreachable_recorded"] is False
        assert payload["sequence"]["started"] is True and payload["sequence"]["skipped"] is None
        rows = await _sequences(session)
        assert len(rows) == 1 and rows[0].temporal_workflow_id == payload["sequence"]["workflow_id"]
        assert (
            temporal.workflows[rows[0].temporal_workflow_id].workflow_id
            == rows[0].temporal_workflow_id
        )
        # A second A2 pass (re-research) does not start a second sequence.
        again = await discovery.run_a2(str(inv.id))
        assert again["sequence"] == {
            "started": False,
            "sequence_id": None,
            "workflow_id": None,
            "skipped": "has_sequence",
        }
        # The pilot's opt-out: research without starting.
        other = await make_investor("Bayou Capital Partners LLC", market_id=market.id, score=88)
        await session.commit()
        quiet = await discovery.run_a2(str(other.id), start_sequence=False)
        assert quiet["primary_email"] and "sequence" not in quiet
        assert len(await _sequences(session)) == 1
    finally:
        discovery.reset()


async def test_run_a2_records_a_qualified_investor_without_email_as_unreachable(
    ctx: starter.StarterContext,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    market: Market,
    make_investor: MakeInvestor,
) -> None:
    inv = await make_investor("Gulf Coast Rentals LLC", market_id=market.id, score=76)
    unqualified = await make_investor("Nobody Knows LLC", market_id=market.id, score=30)
    await session.commit()
    discovery.configure(session_factory, mock_data_providers())
    try:
        payload = await discovery.run_a2(str(inv.id))
        assert payload["primary_email"] is None
        assert payload["unreachable"] is True and payload["unreachable_recorded"] is True
        assert "sequence" not in payload
        again = await discovery.run_a2(str(inv.id))
        assert again["unreachable"] is True and again["unreachable_recorded"] is False
        low = await discovery.run_a2(str(unqualified.id))
        assert low["unreachable"] is False  # not qualified: not an outreach loss
    finally:
        discovery.reset()
    events = (
        (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == starter.EVENT_INVESTOR_UNREACHABLE
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1 and events[0].entity_id == inv.id
    assert events[0].payload["priority"] is True and events[0].payload["score"] == 76
    assert "candidates" in events[0].payload["reasons"]
    assert await _sequences(session) == []
    # The KPI the digest shows (GAP-07).
    assert await qualified_unreachable(session, NOW.replace(year=2020), NOW.replace(year=2030)) == 1


# -- kickoff activity, workflow, schedule --------------------------------------------------------


async def test_kickoff_covers_every_active_or_watch_market(
    ctx: starter.StarterContext,
    session: AsyncSession,
    market: Market,
    make_investor: MakeInvestor,
) -> None:
    active = Market(name="Columbus", state="OH", tier=MarketTier.ACTIVE)
    excluded = Market(name="Elsewhere", state="ZZ", tier=MarketTier.EXCLUDED)
    session.add_all([active, excluded])
    await session.flush()
    await _investor(session, make_investor, market, "Houston LLC", score=70, email="h@x.test")
    await _investor(session, make_investor, active, "Columbus LLC", score=70, email="c@x.test")
    await _investor(session, make_investor, excluded, "Elsewhere LLC", score=70, email="e@x.test")
    result = await starter.run_outreach_kickoff(starter.OutreachKickoffInput())
    assert result.started == 2
    assert [m.market for m in result.markets] == ["Columbus", "Houston"]  # Active first
    assert result.to_json()["started"] == 2
    one = await starter.run_outreach_kickoff(starter.OutreachKickoffInput(market_id=str(market.id)))
    assert one.started == 0 and one.markets[0].skipped == {"has_sequence": 1}


def test_kickoff_workflow_schedule_and_worker_registration() -> None:
    spec = get_schedule("outreach-kickoff")
    assert spec in SCHEDULES and spec.workflow == "OutreachKickoffWorkflow"
    assert spec.interval.total_seconds() == 86_400 and spec.overlap == "skip"
    assert "WF-02" in spec.requirement and "outreach" in spec.tags
    registered = {workflow._Definition.from_class(w).name for w in WORKFLOWS}  # type: ignore[union-attr]
    assert spec.workflow in registered and OutreachKickoffWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert {"outreach_kickoff", "outreach_start_investor_sequences", "run_a2"} <= names
    defn = workflow._Definition.from_class(OutreachKickoffWorkflow)  # type: ignore[union-attr]
    assert defn is not None and set(defn.queries) == {"status"}


def test_sequence_repository_accepts_a_fixed_id() -> None:
    sid = uuid.uuid4()
    assert investor_sequence_workflow_id(str(sid)) == f"investor-sequence:{sid}"
