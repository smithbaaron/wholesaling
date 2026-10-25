"""Daily digest activity (REQUIREMENTS.md BO-07 "daily digest", BO-21, INT-18).

``compute_and_send_digest`` computes the KPI report for one local day and delivers it through
the configured ``DigestSink``s (log always; Slack when ``SLACK_WEBHOOK_URL`` is set; email
deferred to live mode). Dependencies come from ``DigestActivityContext`` so tests inject the
SQLite session factory and fake sinks.

Idempotency: one ``digest.sent`` audit event per (day, workflow id). A retried activity sees the
existing event and reports ``duplicate=True`` without sending again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.config.settings import Settings, get_settings
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.logging import bind_correlation_id, clear_context, get_logger
from wholesaling.reporting.digest import DigestSink, build_sinks, send_digest
from wholesaling.reporting.kpis import compute_report

log = get_logger(__name__)

Clock = Callable[[], datetime]
DIGEST_SENT_EVENT = "digest.sent"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class DigestActivityContext:
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    sinks: list[DigestSink] = field(default_factory=list)
    clock: Clock = _utcnow


_context: DigestActivityContext | None = None


def set_digest_context(ctx: DigestActivityContext | None) -> None:
    """Install (or clear) the dependency context. Tests call this with fake sinks."""
    global _context
    _context = ctx


def get_digest_context() -> DigestActivityContext:
    global _context
    if _context is None:
        from wholesaling.db.engine import get_session_factory

        settings = get_settings()
        _context = DigestActivityContext(
            session_factory=get_session_factory(), settings=settings, sinks=build_sinks(settings)
        )
    return _context


def _bind_workflow_context() -> str | None:
    if activity.in_activity():
        workflow_id = activity.info().workflow_id
        bind_correlation_id(workflow_id)
        return workflow_id
    return None


@dataclass(slots=True)
class DigestInput:
    """``day`` is ``YYYY-MM-DD`` in the operator timezone. ``None`` means the current local
    day: the digest fires at ``DIGEST_HOUR_LOCAL`` and summarises the day so far plus the
    rolling 7 days ending that day, with the live queue depth."""

    day: str | None = None
    idempotency_key: str | None = None


@dataclass(slots=True)
class DigestResult:
    day: str
    timezone: str
    queue_depth: int
    messages_sent_today: int
    sinks: list[str]
    failed_sinks: list[str]
    duplicate: bool = False
    subject: str = ""


def _resolve_day(value: str | None, tz: str, now: datetime) -> date:
    if value:
        return date.fromisoformat(value)
    return now.astimezone(ZoneInfo(tz)).date()


@activity.defn(name="compute_and_send_digest")
async def compute_and_send_digest(input: DigestInput) -> DigestResult:
    workflow_id = _bind_workflow_context()
    try:
        ctx = get_digest_context()
        now = ctx.clock()
        tz = ctx.settings.operator_timezone
        day = _resolve_day(input.day, tz, now)
        key = input.idempotency_key or (
            f"digest:{day.isoformat()}:{workflow_id}" if workflow_id else None
        )
        async with ctx.session_factory() as session:
            audit = AuditRepository(session)
            if key is not None:
                existing = await audit.get_by_idempotency_key(key)
                if existing is not None:
                    log.info("digest.duplicate", day=day.isoformat(), idempotency_key=key)
                    payload = existing.payload or {}
                    return DigestResult(
                        day=day.isoformat(),
                        timezone=tz,
                        queue_depth=int(payload.get("queue_depth", 0)),
                        messages_sent_today=int(payload.get("messages_sent_today", 0)),
                        sinks=list(payload.get("sinks", [])),
                        failed_sinks=list(payload.get("failed_sinks", [])),
                        duplicate=True,
                        subject=str(payload.get("subject", "")),
                    )
            report = await compute_report(session, day, tz=tz, now=now)
            delivery = await send_digest(report, ctx.sinks)
            sinks = [o.sink for o in delivery.outcomes]
            failed = [o.sink for o in delivery.outcomes if not o.ok]
            await audit.record(
                actor=Actor.SYSTEM,
                event_type=DIGEST_SENT_EVENT,
                entity_type="digest",
                payload={
                    "day": day.isoformat(),
                    "timezone": tz,
                    "subject": delivery.digest.subject,
                    "sinks": sinks,
                    "failed_sinks": failed,
                    "queue_depth": report.queue.depth,
                    "messages_sent_today": report.today.messages_sent,
                    "kpis": report.as_dict(),
                },
                correlation_id=workflow_id,
                idempotency_key=key,
            )
            await session.commit()
        log.info(
            "digest.completed",
            day=day.isoformat(),
            sinks=sinks,
            failed_sinks=failed,
            queue_depth=report.queue.depth,
        )
        return DigestResult(
            day=day.isoformat(),
            timezone=tz,
            queue_depth=report.queue.depth,
            messages_sent_today=report.today.messages_sent,
            sinks=sinks,
            failed_sinks=failed,
            subject=delivery.digest.subject,
        )
    finally:
        clear_context()


__all__ = [
    "DIGEST_SENT_EVENT",
    "DigestActivityContext",
    "DigestInput",
    "DigestResult",
    "compute_and_send_digest",
    "get_digest_context",
    "set_digest_context",
]
