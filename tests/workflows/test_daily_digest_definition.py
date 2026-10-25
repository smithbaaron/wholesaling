"""DailyDigestWorkflow contract, schedule registry entry, worker registration, and the
``compute_and_send_digest`` activity against SQLite with fake sinks (no Temporal server)."""

from __future__ import annotations

import inspect
from dataclasses import asdict, fields
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity, workflow

from tests.reporting.conftest import NOW, TZ, seed_kpis
from wholesaling.activities.digest import (
    DIGEST_SENT_EVENT,
    DigestActivityContext,
    DigestInput,
    DigestResult,
    compute_and_send_digest,
    set_digest_context,
)
from wholesaling.config.settings import Settings
from wholesaling.db.models.ops import AuditEvent
from wholesaling.reporting.digest import LogSink, RenderedDigest
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows.daily_digest import DailyDigestInput, DailyDigestWorkflow
from wholesaling.workflows.schedules import SCHEDULES, digest_calendar_spec, get_schedule

SF = async_sessionmaker[AsyncSession]


def _defn() -> workflow._Definition:
    d = workflow._Definition.from_class(DailyDigestWorkflow)
    assert d is not None
    return d


def test_workflow_type_name_and_query() -> None:
    d = _defn()
    assert d.name == "DailyDigestWorkflow"
    assert "status" in d.queries
    assert d.signals == {}


def test_run_takes_a_single_dataclass_input() -> None:
    params = list(inspect.signature(DailyDigestWorkflow.run).parameters)
    assert params == ["self", "input"]
    assert [f.name for f in fields(DailyDigestInput)] == ["day"]
    assert DailyDigestInput().day is None


def test_status_before_run() -> None:
    wf = DailyDigestWorkflow()
    assert wf.status() == {"state": "pending", "result": None}


def test_registered_in_worker() -> None:
    assert DailyDigestWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert "compute_and_send_digest" in names
    assert compute_and_send_digest in ACTIVITIES


def test_schedule_registry_entry() -> None:
    spec = get_schedule("daily-digest")
    assert spec in SCHEDULES
    assert spec.workflow == "DailyDigestWorkflow"
    assert spec.interval == timedelta(days=1)
    assert spec.overlap == "skip"
    assert spec.args == (DailyDigestInput(),)
    assert "DIGEST_HOUR_LOCAL" in spec.note and "OPERATOR_TIMEZONE" in spec.note
    assert "not time-gated" in spec.note
    assert spec.workflow_id_prefix == "sched-daily-digest"
    assert {"digest", "reporting"} <= spec.tags


def test_digest_calendar_spec_from_settings() -> None:
    s = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    assert digest_calendar_spec(s) == {"hour": 7, "minute": 0, "time_zone": "America/Chicago"}
    s = Settings(  # type: ignore[call-arg]
        _env_file=None, app_env="test", digest_hour_local=6, operator_timezone="America/New_York"
    )
    assert digest_calendar_spec(s) == {"hour": 6, "minute": 0, "time_zone": "America/New_York"}


def test_digest_hour_is_validated() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="test", digest_hour_local=24)  # type: ignore[call-arg]


# --------------------------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------------------------


class Collector:
    name = "collector"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.got: list[RenderedDigest] = []

    async def send(self, digest: RenderedDigest) -> None:
        if self.fail:
            raise RuntimeError("sink down")
        self.got.append(digest)


def _ctx(session_factory: SF, *sinks: Any) -> DigestActivityContext:
    settings = Settings(_env_file=None, app_env="test", operator_timezone=TZ)  # type: ignore[call-arg]
    return DigestActivityContext(
        session_factory=session_factory, settings=settings, sinks=list(sinks), clock=lambda: NOW
    )


async def _digest_events(sf: SF) -> list[AuditEvent]:
    async with sf() as s:
        stmt = select(AuditEvent).where(AuditEvent.event_type == DIGEST_SENT_EVENT)
        return list((await s.execute(stmt)).scalars().all())


async def test_activity_computes_sends_and_audits(
    session_factory: SF, session: AsyncSession
) -> None:
    await seed_kpis(session)
    log_sink, collector = LogSink(), Collector()
    set_digest_context(_ctx(session_factory, log_sink, collector))
    try:
        result = await compute_and_send_digest(DigestInput(idempotency_key="digest:test:1"))
    finally:
        set_digest_context(None)
    assert isinstance(result, DigestResult)
    assert result.day == "2026-10-20"  # NOW (14:00 UTC) is 09:00 local on the 20th
    assert result.timezone == TZ
    assert result.queue_depth == 3
    assert result.messages_sent_today == 3
    assert result.sinks == ["log", "collector"]
    assert result.failed_sinks == []
    assert result.duplicate is False
    assert result.subject.startswith("Wholesaling digest 2026-10-20")
    assert len(log_sink.sent) == 1 and len(collector.got) == 1
    assert "$12,000.00" in collector.got[0].text

    events = await _digest_events(session_factory)
    assert len(events) == 1
    assert events[0].idempotency_key == "digest:test:1"
    assert events[0].payload["queue_depth"] == 3
    assert events[0].payload["kpis"]["today"]["messages_sent"] == 3


async def test_activity_explicit_day(session_factory: SF, session: AsyncSession) -> None:
    await seed_kpis(session)
    set_digest_context(_ctx(session_factory))
    try:
        result = await compute_and_send_digest(DigestInput(day="2026-10-18"))
    finally:
        set_digest_context(None)
    assert result.day == "2026-10-18"
    assert result.messages_sent_today == 2  # the two sends from "two days ago"
    assert result.sinks == []


async def test_activity_is_idempotent_on_retry(session_factory: SF) -> None:
    collector = Collector()
    set_digest_context(_ctx(session_factory, collector))
    try:
        first = await compute_and_send_digest(DigestInput(idempotency_key="digest:k"))
        second = await compute_and_send_digest(DigestInput(idempotency_key="digest:k"))
    finally:
        set_digest_context(None)
    assert first.duplicate is False and second.duplicate is True
    assert second.day == first.day and second.sinks == first.sinks
    assert len(collector.got) == 1  # not sent twice
    assert len(await _digest_events(session_factory)) == 1


async def test_activity_reports_failed_sink_without_raising(session_factory: SF) -> None:
    set_digest_context(_ctx(session_factory, Collector(fail=True), LogSink()))
    try:
        result = await compute_and_send_digest(DigestInput())
    finally:
        set_digest_context(None)
    assert result.sinks == ["collector", "log"]
    assert result.failed_sinks == ["collector"]
    events = await _digest_events(session_factory)
    assert events[0].payload["failed_sinks"] == ["collector"]


def test_result_is_a_plain_dataclass_for_the_converter() -> None:
    r = DigestResult(
        day="2026-10-20",
        timezone=TZ,
        queue_depth=0,
        messages_sent_today=0,
        sinks=[],
        failed_sinks=[],
    )
    assert asdict(r)["duplicate"] is False
