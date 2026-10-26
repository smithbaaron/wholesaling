"""BO-21 KPI queries against seeded data: per-day and rolling 7-day windows, queue stats."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from tests.reporting.conftest import DAY, NOW, TZ, KpiSeed
from wholesaling.reporting.kpis import (
    KpiSnapshot,
    compute_report,
    day_window,
    is_compliance_flag,
    queue_stats,
    snapshot,
)


def test_day_window_converts_local_day_to_utc() -> None:
    start, end = day_window(date(2026, 10, 20), TZ)  # CDT, UTC-5
    assert start == datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 21, 5, 0, tzinfo=UTC)
    start, end = day_window(date(2026, 1, 15), TZ)  # CST, UTC-6
    assert start == datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
    assert end - start == timedelta(days=1)


def test_day_window_handles_dst_transition() -> None:
    # 2026-11-01: US DST ends, the local day is 25 hours long.
    start, end = day_window(date(2026, 11, 1), TZ)
    assert end - start == timedelta(hours=25)


async def test_empty_database_gives_zeros(session: AsyncSession) -> None:
    start, end = day_window(DAY, TZ)
    snap = await snapshot(session, start, end)
    assert snap.discovered_contacts == 0
    assert snap.messages_sent == 0
    assert snap.model_spend_usd == Decimal("0")
    assert snap.assignment_fees_usd == Decimal("0")
    assert snap.cost_per_qualified_buyer_usd is None
    assert snap.response_rate is None
    q = await queue_stats(session, NOW)
    assert q.depth == 0 and q.oldest_age_seconds is None


async def test_today_snapshot(session: AsyncSession, seeded: KpiSeed) -> None:
    start, end = day_window(DAY, TZ)
    s = await snapshot(session, start, end)
    assert s.discovered_contacts == 2
    assert s.qualified_unreachable == 1
    assert s.messages_sent == 3
    assert s.responses == 3
    assert s.qualified_responses == 1
    assert s.opt_outs == 1
    assert s.qualified_buy_boxes == 1  # superseded v1 and the inactive box are not counted
    assert s.matching_properties == 2
    assert s.reviewed_deals == 1
    assert s.contracts == 1
    assert s.assignment_fees_usd == Decimal("12000")
    assert s.model_spend_usd == Decimal("0.75")
    assert s.cost_per_qualified_buyer_usd == Decimal("0.75")
    assert s.qa_reviews == 2 and s.qa_blocked == 1
    assert s.drafts_queued == 2
    assert s.drafts_auto_sent == 3
    assert s.response_rate == 1.0
    assert s.opt_out_rate == 1 / 3


async def test_rolling_7d_snapshot(session: AsyncSession, seeded: KpiSeed) -> None:
    _, end = day_window(DAY, TZ)
    s = await snapshot(session, end - timedelta(days=7), end)
    assert s.discovered_contacts == 3  # the 10-day-old contact is outside
    assert s.qualified_unreachable == 2  # the 20-day-old one is outside
    assert s.messages_sent == 5  # the 9-day-old send is outside
    assert s.responses == 4
    assert s.qualified_responses == 2
    assert s.qualified_buy_boxes == 2
    assert s.matching_properties == 3
    assert s.reviewed_deals == 2
    assert s.contracts == 1  # the 8-day-old acceptance is outside
    assert s.assignment_fees_usd == Decimal("12000")
    assert s.model_spend_usd == Decimal("1.75")
    assert s.cost_per_qualified_buyer_usd == Decimal("0.875")
    assert s.qa_reviews == 3 and s.qa_blocked == 1
    assert s.drafts_queued == 3
    assert s.drafts_auto_sent == 5


async def test_queue_stats_depth_and_oldest(session: AsyncSession, seeded: KpiSeed) -> None:
    q = await queue_stats(session, NOW)
    assert q.depth == 3
    # Oldest queued draft was created 2 days ago at 10:00 local = 15:00 UTC; NOW is 14:00 UTC.
    assert q.oldest_age_seconds == 47 * 3600


async def test_compute_report_assembles_everything(session: AsyncSession, seeded: KpiSeed) -> None:
    report = await compute_report(session, DAY, tz=TZ, now=NOW)
    assert report.day == DAY and report.timezone == TZ
    assert report.today.messages_sent == 3
    assert report.rolling_7d.messages_sent == 5
    assert report.queue.depth == 3
    assert len(report.readiness) == 12
    data = report.as_dict()
    assert data["day"] == "2026-10-20"
    assert data["today"]["cost_per_qualified_buyer_usd"] == "0.7500"
    assert Decimal(data["rolling_7d"]["assignment_fees_usd"]) == Decimal("12000")
    assert data["queue"] == {"depth": 3, "oldest_age_seconds": 47 * 3600.0}
    assert {r["action"] for r in data["readiness"]} == {r.action.value for r in report.readiness}


async def test_compute_report_can_skip_readiness(session: AsyncSession) -> None:
    report = await compute_report(session, DAY, tz=TZ, now=NOW, include_readiness=False)
    assert report.readiness == []


def test_snapshot_as_dict_serialises_decimals_as_strings() -> None:
    snap = KpiSnapshot(
        window_start=NOW, window_end=NOW, qualified_buy_boxes=4, model_spend_usd=Decimal("1")
    )
    d = snap.as_dict()
    assert d["model_spend_usd"] == "1"
    assert d["cost_per_qualified_buyer_usd"] == "0.2500"
    assert d["window_start"] == NOW.isoformat()


def test_is_compliance_flag() -> None:
    assert is_compliance_flag("compliance:missing_opt_out")
    assert is_compliance_flag("prohibited_claim")
    assert is_compliance_flag("legal_question")
    assert not is_compliance_flag("tone:salesy")
    assert not is_compliance_flag("length")
