"""WF-02 sequence starter: qualified investor with a primary email -> ``sequences`` row ->
``InvestorSequenceWorkflow`` (REQUIREMENTS.md INV-04, INV-08, INV-10, WF-02, POL-03l).

Three entry points share one rule set (``list_startable_investors`` / ``start_investor_sequence``):

- ``maybe_start_sequence_for_investor``   called at the end of ``run_a2`` (discovery.py) so an
                                           investor whose primary email was just found starts
                                           the same day;
- ``start_investor_sequences``             activity: one market, priority tier first (INV-04),
                                           bounded by ``limit`` and the daily kickoff cap;
- ``run_outreach_kickoff``                 activity: every Active / Watch market (or one), the
                                           daily catch-up the ``outreach-kickoff`` schedule runs.

Eligibility: score >= ``PROSPECT_THRESHOLD`` (50), not excluded, market tier Active or Watch, a
primary email contact that is not suppressed, and no investor sequence on record (an earlier
sequence that completed or was cancelled by a reply / opt-out / bounce / human is final: nothing
here re-engages; ``config/sequences.yaml``'s requeue rules are a later build step). A sequence
whose workflow start failed (``cancel_reason = "start_failed"``) does not count, so a retried
kickoff starts it again.

Workflow id: ``investor_sequence_workflow_id(sequence_id)`` = ``investor-sequence:{sequence_id}``,
with the sequence id generated here so the row carries its final workflow id from the first
flush (``SequenceRepository.create`` is idempotent on that id). Order of operations: row + audit
committed, then ``start_workflow`` (idempotent on the id in every gateway: a retry re-uses the
running workflow). If the start raises, the row is cancelled ``start_failed`` and the error
re-raised so Temporal retries the activity.

Daily cap: ``config/caps.yaml`` ``outreach_kickoff_daily_cap`` when set, else
``Settings.outreach_kickoff_daily_cap`` (default 50): at most that many investor sequences are
*started* per UTC day across all markets. The send-side caps (POL-03a, ``sends_today``) are
separate and stay in the policy engine.

Dependencies come from ``StarterContext`` (session factory, Temporal gateway, settings, clock);
tests and the synthetic pilot inject SQLite plus the mock / simulated gateway.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.agents.scoring import PRIORITY_THRESHOLD, PROSPECT_THRESHOLD
from wholesaling.config.settings import Settings
from wholesaling.config.tunables import get_tunables
from wholesaling.db.models.enums import (
    Actor,
    ContactChannel,
    MarketTier,
    SequenceKind,
    SequenceStatus,
)
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Sequence
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.db.repositories.messages import MessageRepository
from wholesaling.db.repositories.sequences import SequenceRepository
from wholesaling.integrations.temporal import TemporalGateway, get_temporal
from wholesaling.logging import bind_correlation_id, clear_context, get_logger

if TYPE_CHECKING:
    from wholesaling.workflows.investor_sequence import InvestorSequenceInput

log = get_logger(__name__)

Clock = Callable[[], datetime]

STARTABLE_TIERS: frozenset[MarketTier] = frozenset({MarketTier.ACTIVE, MarketTier.WATCH})
START_FAILED_REASON = "start_failed"
EVENT_SEQUENCE_STARTED = "sequence.started"
EVENT_INVESTOR_UNREACHABLE = "investor.unreachable"
STARTER_ACTOR_ID = "sequence_starter"

SKIP_NO_EMAIL = "no_email"
SKIP_SUPPRESSED = "suppressed"
SKIP_HAS_SEQUENCE = "has_sequence"
SKIP_NOT_QUALIFIED = "not_qualified"
SKIP_MARKET_TIER = "market_tier"
SKIP_CAP = "daily_cap"
SKIP_LIMIT = "limit"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


# --------------------------------------------------------------------------------------------
# Dependency context
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class StarterContext:
    session_factory: async_sessionmaker[AsyncSession]
    temporal: TemporalGateway
    settings: Settings
    clock: Clock = _utcnow


_context: StarterContext | None = None


def set_starter_context(ctx: StarterContext | None) -> None:
    """Install (or clear) the dependency context (tests, synthetic pilot)."""
    global _context
    _context = ctx


def get_starter_context() -> StarterContext:
    global _context
    if _context is None:
        from wholesaling.config.settings import get_settings
        from wholesaling.db.engine import get_session_factory

        settings = get_settings()
        _context = StarterContext(
            session_factory=get_session_factory(),
            temporal=get_temporal(settings),
            settings=settings,
        )
    return _context


def _bind() -> None:
    if activity.in_activity():
        bind_correlation_id(activity.info().workflow_id)


# --------------------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------------------


def daily_kickoff_cap(settings: Settings) -> int:
    """``config/caps.yaml`` ``outreach_kickoff_daily_cap`` when present, else the setting."""
    from_yaml = get_tunables().caps.outreach_kickoff_daily_cap
    return int(from_yaml if from_yaml is not None else settings.outreach_kickoff_daily_cap)


def utc_day_start(now: datetime) -> datetime:
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return datetime.combine(aware.astimezone(UTC).date(), time.min, tzinfo=UTC)


async def sequences_started_today(session: AsyncSession, now: datetime) -> int:
    """Investor sequences created since UTC midnight of ``now`` (the daily kickoff cap);
    ``start_investor_sequence`` stamps ``created_at`` from the same clock."""
    stmt = (
        select(func.count())
        .select_from(Sequence)
        .where(
            Sequence.kind == SequenceKind.INVESTOR,
            Sequence.created_at >= utc_day_start(now),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


def is_qualified(investor: Investor) -> bool:
    return (
        investor.excluded_reason is None
        and investor.score is not None
        and investor.score >= PROSPECT_THRESHOLD
    )


@dataclass(slots=True)
class StartCandidate:
    investor_id: uuid.UUID
    investor_name: str
    market_id: uuid.UUID
    market: str
    score: int
    email: str

    @property
    def priority(self) -> bool:
        return self.score >= PRIORITY_THRESHOLD

    def to_json(self) -> dict[str, Any]:
        return {
            "investor_id": str(self.investor_id),
            "investor_name": self.investor_name,
            "market": self.market,
            "score": self.score,
            "priority": self.priority,
            "email": self.email,
        }


async def primary_email(session: AsyncSession, investor_id: uuid.UUID) -> str | None:
    stmt = (
        select(Contact.value)
        .where(
            Contact.investor_id == investor_id,
            Contact.channel == ContactChannel.EMAIL,
            Contact.is_primary.is_(True),
        )
        .limit(1)
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    return str(value) if value else None


async def has_sequence_on_record(session: AsyncSession, investor_id: uuid.UUID) -> bool:
    """True when any investor sequence exists that was not cancelled as ``start_failed``."""
    stmt = select(Sequence.status, Sequence.cancel_reason).where(
        Sequence.kind == SequenceKind.INVESTOR, Sequence.investor_id == investor_id
    )
    for status, reason in (await session.execute(stmt)).all():
        if status is SequenceStatus.CANCELLED and reason == START_FAILED_REASON:
            continue
        return True
    return False


async def skip_reason(session: AsyncSession, investor: Investor, market: Market) -> str | None:
    """Why this investor cannot start a sequence now, or ``None`` when it can."""
    if not is_qualified(investor):
        return SKIP_NOT_QUALIFIED
    if market.tier not in STARTABLE_TIERS:
        return SKIP_MARKET_TIER
    email = await primary_email(session, investor.id)
    if email is None:
        return SKIP_NO_EMAIL
    if await MessageRepository(session).is_suppressed(email):
        return SKIP_SUPPRESSED
    if await has_sequence_on_record(session, investor.id):
        return SKIP_HAS_SEQUENCE
    return None


async def list_startable_investors(
    session: AsyncSession, market_id: uuid.UUID, *, limit: int | None = None
) -> tuple[list[StartCandidate], dict[str, int]]:
    """Eligible investors of one market, priority tier first (score desc, then name), plus a
    histogram of why the others were skipped."""
    market = await session.get(Market, market_id)
    if market is None:
        msg = f"unknown market {market_id}"
        raise LookupError(msg)
    stmt = (
        select(Investor)
        .where(
            Investor.market_id == market_id,
            Investor.excluded_reason.is_(None),
            Investor.score.is_not(None),
            Investor.score >= PROSPECT_THRESHOLD,
        )
        .order_by(Investor.score.desc(), Investor.canonical_name, Investor.id)
    )
    investors = list((await session.execute(stmt)).scalars().all())
    candidates: list[StartCandidate] = []
    skipped: dict[str, int] = {}
    for investor in investors:
        reason = await skip_reason(session, investor, market)
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        if limit is not None and len(candidates) >= limit:
            skipped[SKIP_LIMIT] = skipped.get(SKIP_LIMIT, 0) + 1
            continue
        email = await primary_email(session, investor.id)
        candidates.append(
            StartCandidate(
                investor_id=investor.id,
                investor_name=investor.canonical_name,
                market_id=market.id,
                market=market.name,
                score=int(investor.score or 0),
                email=email or "",
            )
        )
    return candidates, skipped


# --------------------------------------------------------------------------------------------
# Starting one sequence
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class StartedSequence:
    sequence_id: str
    workflow_id: str
    investor_id: str
    market: str
    email: str
    priority: bool


def sequence_plan() -> list[int]:
    return [int(d) for d in get_tunables().sequences.investor_followup_days]


def _sequence_workflow() -> tuple[str, type[InvestorSequenceInput], Callable[[str], str]]:
    # Imported here, not at module level: ``workflows/__init__`` imports every workflow, and
    # ``workflows/outreach_kickoff.py`` imports this module, so a top-level import would cycle.
    from wholesaling.workflows.investor_sequence import (
        WORKFLOW_NAME,
        InvestorSequenceInput,
        investor_sequence_workflow_id,
    )

    return WORKFLOW_NAME, InvestorSequenceInput, investor_sequence_workflow_id


async def start_investor_sequence(
    ctx: StarterContext, candidate: StartCandidate, *, honor_send_window: bool = True
) -> StartedSequence:
    """Create the ``sequences`` row (committed) and start its workflow."""
    workflow_name, input_type, workflow_id_for = _sequence_workflow()
    intervals = sequence_plan()
    sequence_id = uuid.uuid4()
    workflow_id = workflow_id_for(str(sequence_id))
    async with ctx.session_factory() as session:
        repo = SequenceRepository(session)
        row, created = await repo.create(
            kind=SequenceKind.INVESTOR,
            investor_id=candidate.investor_id,
            temporal_workflow_id=workflow_id,
            total_steps=len(intervals) + 1,
            sequence_id=sequence_id,
        )
        # Stamped from the starter's clock (not the DB default) so ``sequences_started_today``
        # counts on the same clock this activity reads - the simulated one in the pilot.
        row.created_at = ctx.clock()
        await AuditRepository(session).record(
            actor=Actor.SYSTEM,
            actor_id=STARTER_ACTOR_ID,
            event_type=EVENT_SEQUENCE_STARTED,
            entity_type="sequence",
            entity_id=row.id,
            payload={
                **candidate.to_json(),
                "workflow_id": workflow_id,
                "intervals_days": intervals,
            },
            correlation_id=workflow_id,
            idempotency_key=f"seq:{row.id}:started",
        )
        await session.commit()
    try:
        await ctx.temporal.start_workflow(
            workflow_name,
            [
                input_type(
                    sequence_id=str(sequence_id),
                    investor_id=str(candidate.investor_id),
                    contact_email=candidate.email,
                    market=candidate.market,
                    intervals_days=list(intervals),
                    honor_send_window=honor_send_window,
                )
            ],
            workflow_id=workflow_id,
        )
    except Exception as exc:
        log.error(
            "sequence_starter.start_failed",
            sequence_id=str(sequence_id),
            investor_id=str(candidate.investor_id),
            error=str(exc)[:300],
        )
        async with ctx.session_factory() as session:
            await SequenceRepository(session).cancel(
                sequence_id, START_FAILED_REASON, at=ctx.clock()
            )
            await session.commit()
        raise
    log.info(
        "sequence_starter.started",
        sequence_id=str(sequence_id),
        workflow_id=workflow_id,
        investor_id=str(candidate.investor_id),
        market=candidate.market,
        priority=candidate.priority,
        created=created,
    )
    return StartedSequence(
        sequence_id=str(sequence_id),
        workflow_id=workflow_id,
        investor_id=str(candidate.investor_id),
        market=candidate.market,
        email=candidate.email,
        priority=candidate.priority,
    )


async def record_unreachable(
    session: AsyncSession, investor: Investor, *, reasons: dict[str, Any]
) -> bool:
    """``investor.unreachable``: a qualified investor A2 found no email for (pilot GAP-07).
    One event per investor (idempotency key); returns whether this call wrote it."""
    audit = AuditRepository(session)
    key = f"a2:{investor.id}:unreachable"
    if await audit.get_by_idempotency_key(key) is not None:
        return False
    await audit.record(
        actor=Actor.AGENT,
        actor_id="A2",
        event_type=EVENT_INVESTOR_UNREACHABLE,
        entity_type="investor",
        entity_id=investor.id,
        payload={
            "investor_id": str(investor.id),
            "investor_name": investor.canonical_name,
            "market_id": str(investor.market_id) if investor.market_id else None,
            "score": investor.score,
            "priority": bool(investor.score is not None and investor.score >= PRIORITY_THRESHOLD),
            "reasons": reasons,
        },
        correlation_id=str(investor.id),
        idempotency_key=key,
    )
    log.warning(
        "a2.investor_unreachable",
        investor_id=str(investor.id),
        score=investor.score,
        reasons=reasons,
    )
    return True


async def maybe_start_sequence_for_investor(
    ctx: StarterContext, investor_id: uuid.UUID
) -> tuple[StartedSequence | None, str | None]:
    """Start the investor's sequence if the rules allow; ``(started, skip_reason)``."""
    async with ctx.session_factory() as session:
        investor = await session.get(Investor, investor_id)
        if investor is None:
            msg = f"unknown investor {investor_id}"
            raise LookupError(msg)
        market = await session.get(Market, investor.market_id) if investor.market_id else None
        if market is None:
            return None, SKIP_MARKET_TIER
        reason = await skip_reason(session, investor, market)
        if reason is not None:
            return None, reason
        now = ctx.clock()
        if await sequences_started_today(session, now) >= daily_kickoff_cap(ctx.settings):
            return None, SKIP_CAP
        email = await primary_email(session, investor.id)
        candidate = StartCandidate(
            investor_id=investor.id,
            investor_name=investor.canonical_name,
            market_id=market.id,
            market=market.name,
            score=int(investor.score or 0),
            email=email or "",
        )
    return await start_investor_sequence(ctx, candidate), None


# --------------------------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class StartSequencesInput:
    market_id: str
    limit: int | None = None


@dataclass(slots=True)
class StartSequencesResult:
    market_id: str
    market: str | None
    considered: int
    started: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    daily_cap: int = 0
    started_today_before: int = 0

    @property
    def started_count(self) -> int:
        return len(self.started)


async def _start_for_market(
    ctx: StarterContext, market_id: uuid.UUID, limit: int | None
) -> StartSequencesResult:
    async with ctx.session_factory() as session:
        market = await session.get(Market, market_id)
        if market is None:
            msg = f"unknown market {market_id}"
            raise LookupError(msg)
        if market.tier not in STARTABLE_TIERS:
            return StartSequencesResult(
                market_id=str(market_id),
                market=market.name,
                considered=0,
                skipped={SKIP_MARKET_TIER: 1},
                daily_cap=daily_kickoff_cap(ctx.settings),
            )
        candidates, skipped = await list_startable_investors(session, market_id, limit=limit)
        now = ctx.clock()
        cap = daily_kickoff_cap(ctx.settings)
        today = await sequences_started_today(session, now)
        name = market.name
    room = max(0, cap - today)
    if len(candidates) > room:
        skipped[SKIP_CAP] = skipped.get(SKIP_CAP, 0) + len(candidates) - room
        candidates = candidates[:room]
    result = StartSequencesResult(
        market_id=str(market_id),
        market=name,
        considered=len(candidates) + sum(skipped.values()),
        skipped=skipped,
        daily_cap=cap,
        started_today_before=today,
    )
    for candidate in candidates:  # already priority tier first (INV-04)
        started = await start_investor_sequence(ctx, candidate)
        result.started.append(asdict(started))
    return result


@activity.defn(name="outreach_start_investor_sequences")
async def start_investor_sequences(input: StartSequencesInput) -> StartSequencesResult:
    """WF-02 for one market: every eligible investor, priority tier first, within the cap."""
    _bind()
    ctx = get_starter_context()
    try:
        result = await _start_for_market(ctx, uuid.UUID(input.market_id), input.limit)
        log.info(
            "sequence_starter.market_done",
            market_id=input.market_id,
            started=result.started_count,
            skipped=result.skipped,
        )
        return result
    finally:
        clear_context()


@dataclass(slots=True)
class OutreachKickoffInput:
    market_id: str | None = None  # None = every Active / Watch market
    limit_per_market: int | None = None


@dataclass(slots=True)
class OutreachKickoffResult:
    markets: list[StartSequencesResult] = field(default_factory=list)

    @property
    def started(self) -> int:
        return sum(m.started_count for m in self.markets)

    def to_json(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "markets": [{**asdict(m), "started_count": m.started_count} for m in self.markets],
        }


@activity.defn(name="outreach_kickoff")
async def run_outreach_kickoff(input: OutreachKickoffInput) -> OutreachKickoffResult:
    """Daily catch-up: start sequences for every Active / Watch market (Active first)."""
    _bind()
    ctx = get_starter_context()
    try:
        if input.market_id is not None:
            market_ids = [uuid.UUID(input.market_id)]
        else:
            async with ctx.session_factory() as session:
                stmt = (
                    select(Market.id)
                    .where(Market.tier.in_(list(STARTABLE_TIERS)))
                    .order_by(Market.tier, Market.name)
                )
                market_ids = list((await session.execute(stmt)).scalars().all())
        result = OutreachKickoffResult()
        for market_id in market_ids:
            result.markets.append(await _start_for_market(ctx, market_id, input.limit_per_market))
        log.info("sequence_starter.kickoff_done", started=result.started, markets=len(market_ids))
        return result
    finally:
        clear_context()


__all__ = [
    "EVENT_INVESTOR_UNREACHABLE",
    "EVENT_SEQUENCE_STARTED",
    "SKIP_CAP",
    "SKIP_HAS_SEQUENCE",
    "SKIP_LIMIT",
    "SKIP_MARKET_TIER",
    "SKIP_NOT_QUALIFIED",
    "SKIP_NO_EMAIL",
    "SKIP_SUPPRESSED",
    "STARTABLE_TIERS",
    "START_FAILED_REASON",
    "OutreachKickoffInput",
    "OutreachKickoffResult",
    "StartCandidate",
    "StartSequencesInput",
    "StartSequencesResult",
    "StartedSequence",
    "StarterContext",
    "daily_kickoff_cap",
    "get_starter_context",
    "has_sequence_on_record",
    "is_qualified",
    "list_startable_investors",
    "maybe_start_sequence_for_investor",
    "primary_email",
    "record_unreachable",
    "run_outreach_kickoff",
    "sequences_started_today",
    "set_starter_context",
    "skip_reason",
    "start_investor_sequence",
    "start_investor_sequences",
]
