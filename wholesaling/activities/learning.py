"""Learning-loop activity (REQUIREMENTS.md BO-13 "learning loop on outcomes", INV-11, M1-05).

``compute_learning_stats`` reads the last ``window_days`` of outcomes (read-only,
``reporting.learning.load_outcomes``), computes the per-template reply-rate stats and the
per-market realized results, and persists them as one ``learning.stats`` audit event per day.
The event payload is the ``LearningStats.as_dict()`` document; the digest, M1's feedback step
and the operator read it from there. Nothing else is written: no template is retired, no market
tier changes (M1-05's threshold is OPN-11).

Idempotency: one event per (day, workflow id). A retried activity sees the existing event and
reports ``duplicate=True``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.config.settings import Settings, get_settings
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.logging import bind_correlation_id, clear_context, get_logger
from wholesaling.reporting.learning import (
    MarketThresholds,
    load_outcomes,
    markets_below_threshold,
)
from wholesaling.reporting.learning import (
    compute_learning_stats as compute_stats,
)

log = get_logger(__name__)

Clock = Callable[[], datetime]
LEARNING_STATS_EVENT = "learning.stats"
DEFAULT_WINDOW_DAYS = 90  # M1-05 "90-day results"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class LearningActivityContext:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    clock: Clock = _utcnow
    thresholds: MarketThresholds | None = None  # OPN-11: none configured by default


_context: LearningActivityContext | None = None


def set_learning_context(ctx: LearningActivityContext | None) -> None:
    """Install (or clear) the dependency context. Tests call this with the SQLite factory."""
    global _context
    _context = ctx


def get_learning_context() -> LearningActivityContext:
    global _context
    if _context is None:
        from wholesaling.db.engine import get_session_factory

        _context = LearningActivityContext(
            session_factory=get_session_factory(), settings=get_settings()
        )
    return _context


def _bind_workflow_context() -> str | None:
    if activity.in_activity():
        workflow_id = activity.info().workflow_id
        bind_correlation_id(workflow_id)
        return workflow_id
    return None


@dataclass(slots=True)
class LearningInput:
    """``day`` is ``YYYY-MM-DD`` in the operator timezone (``None`` = today); the window is the
    ``window_days`` ending at the start of the day after ``day``."""

    day: str | None = None
    window_days: int = DEFAULT_WINDOW_DAYS
    idempotency_key: str | None = None


@dataclass(slots=True)
class LearningResult:
    day: str
    window_start: str
    window_end: str
    templates: int
    markets: int
    touches: int
    replies: int
    buy_boxes: int
    offers_accepted: int
    assignments_closed: int
    markets_below_threshold: list[str]
    duplicate: bool = False


def _window(day: date, tz: str, window_days: int) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    end_local = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=1)
    end = end_local.astimezone(UTC)
    return end - timedelta(days=window_days), end


def _resolve_day(value: str | None, tz: str, now: datetime) -> date:
    if value:
        return date.fromisoformat(value)
    return now.astimezone(ZoneInfo(tz)).date()


@activity.defn(name="compute_learning_stats")
async def compute_learning_stats(input: LearningInput) -> LearningResult:
    workflow_id = _bind_workflow_context()
    try:
        ctx = get_learning_context()
        now = ctx.clock()
        tz = ctx.settings.operator_timezone
        day = _resolve_day(input.day, tz, now)
        start, end = _window(day, tz, input.window_days)
        key = input.idempotency_key or (
            f"learning:{day.isoformat()}:{workflow_id}" if workflow_id else None
        )
        async with ctx.session_factory() as session:
            audit = AuditRepository(session)
            if key is not None:
                existing = await audit.get_by_idempotency_key(key)
                if existing is not None:
                    log.info("learning.duplicate", day=day.isoformat(), idempotency_key=key)
                    payload = existing.payload or {}
                    summary = payload.get("summary") or {}
                    return LearningResult(
                        day=day.isoformat(),
                        window_start=str(payload.get("window_start", start.isoformat())),
                        window_end=str(payload.get("window_end", end.isoformat())),
                        templates=int(summary.get("templates", 0)),
                        markets=int(summary.get("markets", 0)),
                        touches=int(summary.get("touches", 0)),
                        replies=int(summary.get("replies", 0)),
                        buy_boxes=int(summary.get("buy_boxes", 0)),
                        offers_accepted=int(summary.get("offers_accepted", 0)),
                        assignments_closed=int(summary.get("assignments_closed", 0)),
                        markets_below_threshold=list(summary.get("markets_below_threshold", [])),
                        duplicate=True,
                    )
            outcomes = await load_outcomes(session, start, end)
            stats = compute_stats(outcomes, thresholds=ctx.thresholds)
            below = markets_below_threshold(stats.markets)
            summary = {
                "templates": len(stats.templates),
                "markets": len(stats.markets),
                "touches": sum(m.touches_sent for m in stats.markets),
                "replies": sum(m.replies for m in stats.markets),
                "buy_boxes": sum(m.buy_boxes_captured for m in stats.markets),
                "offers_accepted": sum(m.offers_accepted for m in stats.markets),
                "assignments_closed": sum(m.assignments_closed for m in stats.markets),
                "markets_below_threshold": below,
            }
            await audit.record(
                actor=Actor.SYSTEM,
                event_type=LEARNING_STATS_EVENT,
                entity_type="learning",
                payload={
                    "day": day.isoformat(),
                    "timezone": tz,
                    "window_days": input.window_days,
                    "summary": summary,
                    **stats.as_dict(),
                },
                correlation_id=workflow_id,
                idempotency_key=key,
            )
            await session.commit()
        log.info("learning.completed", day=day.isoformat(), **summary)
        return LearningResult(
            day=day.isoformat(),
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            templates=int(summary["templates"]),
            markets=int(summary["markets"]),
            touches=int(summary["touches"]),
            replies=int(summary["replies"]),
            buy_boxes=int(summary["buy_boxes"]),
            offers_accepted=int(summary["offers_accepted"]),
            assignments_closed=int(summary["assignments_closed"]),
            markets_below_threshold=below,
        )
    finally:
        clear_context()


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "LEARNING_STATS_EVENT",
    "LearningActivityContext",
    "LearningInput",
    "LearningResult",
    "compute_learning_stats",
    "get_learning_context",
    "set_learning_context",
]
