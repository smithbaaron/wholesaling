"""Digest rendering (text + HTML), the DigestSink protocol, LogSink, SlackWebhookSink over
httpx.MockTransport, build_sinks from settings, and per-sink failure isolation (INT-18)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.reporting.conftest import DAY, NOW, TZ, KpiSeed
from wholesaling.config.settings import Settings
from wholesaling.policy.actions import PolicyAction
from wholesaling.reporting.digest import (
    DigestSink,
    LogSink,
    RenderedDigest,
    SlackWebhookSink,
    build_sinks,
    digest_subject,
    render_digest,
    send_digest,
)
from wholesaling.reporting.kpis import (
    KpiReport,
    KpiSnapshot,
    PromotionReadiness,
    QueueStats,
    compute_report,
)


def _report(**queue: Any) -> KpiReport:
    today = KpiSnapshot(
        window_start=NOW,
        window_end=NOW,
        discovered_contacts=4,
        messages_sent=12,
        responses=3,
        qualified_responses=2,
        qualified_buy_boxes=2,
        model_spend_usd=Decimal("1.5"),
        assignment_fees_usd=Decimal("12000"),
        contracts=1,
    )
    week = KpiSnapshot(window_start=NOW, window_end=NOW, messages_sent=40, qualified_buy_boxes=5)
    readiness = [
        PromotionReadiness(
            action=a,
            window_days=30,
            approved_drafts=51 if a is PolicyAction.INVESTOR_INITIAL_OUTREACH else 0,
            edited_drafts=1 if a is PolicyAction.INVESTOR_INITIAL_OUTREACH else 0,
            qa_blocked_sent=0,
            compliance_flags=0,
            edit_rate_7d=None,
            hard_floor=a in {PolicyAction.CONTRACT_EXECUTION, PolicyAction.EARNEST_MONEY},
        )
        for a in PolicyAction
    ]
    return KpiReport(
        day=DAY,
        timezone=TZ,
        generated_at=NOW,
        today=today,
        rolling_7d=week,
        queue=QueueStats(**({"depth": 3, "oldest_age_seconds": 2 * 3600 + 120} | queue)),
        readiness=readiness,
    )


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"app_env": "test"}
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------


def test_subject_summarises_queue_and_volume() -> None:
    assert digest_subject(_report()) == (
        "Wholesaling digest 2026-10-20: 3 awaiting review, 12 sent, 3 replies"
    )


def test_render_text_contains_every_kpi_and_queue() -> None:
    d = render_digest(_report())
    assert isinstance(d, RenderedDigest)
    t = d.text
    assert "WHOLESALING DAILY DIGEST — 2026-10-20 (America/Chicago)" in t
    assert "awaiting your decision : 3" in t
    assert "oldest waiting         : 2h 2m" in t
    assert "not time-gated" in t
    for label in [
        "discovered contacts",
        "messages sent",
        "responses",
        "qualified responses",
        "opt-outs",
        "qualified buy boxes",
        "matching properties",
        "reviewed deals",
        "contracts (accepted offers)",
        "assignment fees",
        "model spend",
        "cost / qualified buyer",
    ]:
        assert label in t, label
    assert "$12,000.00" in t
    assert "$1.50" in t
    assert "$0.75" in t  # 1.5 / 2 qualified buy boxes
    assert "[READY] investor_initial_outreach" in t
    assert "contract_execution" in t and "(hard floor)" in t
    assert "nothing auto-promotes" in t


def test_render_html_is_escaped_and_complete() -> None:
    d = render_digest(_report())
    h = d.html
    assert h.startswith("<!doctype html>")
    assert "3 drafts awaiting review" in h
    assert "Cost per qualified buyer" in h
    assert "$0.75" in h
    assert "Promotion readiness (POL-04)" in h
    assert "<strong>ready</strong> · investor initial outreach" in h
    assert "Email delivery of this digest is deferred to live mode" in h


def test_render_handles_empty_queue_and_no_qualified_buyers() -> None:
    report = _report(depth=0, oldest_age_seconds=None)
    report.today.qualified_buy_boxes = 0
    d = render_digest(report)
    assert "0 awaiting review" in d.subject
    assert "oldest waiting         : -" in d.text
    assert "0 drafts awaiting review" in d.html


async def test_render_from_real_seeded_report(session: AsyncSession, seeded: KpiSeed) -> None:
    report = await compute_report(session, DAY, tz=TZ, now=NOW)
    d = render_digest(report)
    assert "3 awaiting review, 3 sent, 3 replies" in d.subject
    assert "$12,000.00" in d.text
    assert "1d 23h" in d.text  # oldest queued draft


# --------------------------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------------------------


async def test_log_sink_records_and_satisfies_protocol() -> None:
    sink = LogSink()
    assert isinstance(sink, DigestSink)
    delivery = await send_digest(_report(), [sink])
    assert delivery.ok
    assert [o.sink for o in delivery.outcomes] == ["log"]
    assert len(sink.sent) == 1
    assert sink.sent[0].subject.startswith("Wholesaling digest")


async def test_slack_sink_posts_blocks_via_mock_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sink = SlackWebhookSink("https://hooks.slack.com/services/T/B/x", client=client)
        assert isinstance(sink, DigestSink)
        delivery = await send_digest(_report(), [sink])
    assert delivery.ok
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == "https://hooks.slack.com/services/T/B/x"
    body = json.loads(req.content)
    assert body["text"].startswith("Wholesaling digest 2026-10-20")
    assert body["blocks"][0]["type"] == "header"
    assert "awaiting your decision : 3" in body["blocks"][1]["text"]["text"]


async def test_slack_sink_failure_is_isolated_and_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    log_sink = LogSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        slack = SlackWebhookSink("https://hooks.slack.com/x", client=client)
        delivery = await send_digest(_report(), [log_sink, slack])
    assert not delivery.ok
    assert [(o.sink, o.ok) for o in delivery.outcomes] == [("log", True), ("slack", False)]
    assert "500" in (delivery.outcomes[1].detail or "")
    assert len(log_sink.sent) == 1  # the log still got it


async def test_slack_sink_truncates_very_long_text() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200)

    digest = render_digest(_report())
    digest.text = "x" * 10_000
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await SlackWebhookSink("https://hooks.slack.com/x", client=client).send(digest)
    text = captured["body"]["blocks"][1]["text"]["text"]
    assert len(text) <= 3010
    assert text.endswith("(truncated)```")


def test_slack_sink_requires_https() -> None:
    with pytest.raises(ValueError, match="https"):
        SlackWebhookSink("http://hooks.slack.com/x")


def test_build_sinks_log_only_by_default() -> None:
    sinks = build_sinks(_settings())
    assert [s.name for s in sinks] == ["log"]


def test_build_sinks_adds_slack_when_configured() -> None:
    sinks = build_sinks(_settings(slack_webhook_url="https://hooks.slack.com/services/a/b/c"))
    assert [s.name for s in sinks] == ["log", "slack"]
    assert build_sinks(_settings(slack_webhook_url="   "))[-1].name == "log"


async def test_custom_sink_only_needs_name_and_send() -> None:
    class Collector:
        name = "collector"

        def __init__(self) -> None:
            self.got: list[str] = []

        async def send(self, digest: RenderedDigest) -> None:
            self.got.append(digest.subject)

    c = Collector()
    assert isinstance(c, DigestSink)
    await send_digest(_report(), [c])
    assert c.got and c.got[0].startswith("Wholesaling digest")


def test_generated_at_is_rendered_in_utc() -> None:
    d = render_digest(_report())
    assert datetime(2026, 10, 20, 14, 0, tzinfo=UTC).strftime("%Y-%m-%d %H:%M UTC") in d.text
