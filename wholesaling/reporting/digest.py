"""The daily digest (REQUIREMENTS.md BO-07 "daily digest", BO-21, INV-19, INT-18).

``render_digest`` turns a ``KpiReport`` into plain text and HTML (Jinja2 templates under
``wholesaling/api/templates/digest/``). ``send_digest`` hands the rendered digest to every
configured ``DigestSink``:

- ``LogSink``            always on: one structured log line with the KPI payload;
- ``SlackWebhookSink``   only when ``SLACK_WEBHOOK_URL`` is set (INT-18 "optional Slack webhook");
- email                  deferred: RUL-06 forbids live mail in this build and the Graph gateway's
                         send path is guarded by ``GRAPH_ALLOW_LIVE_SEND``. When live mode is
                         cleared, an ``EmailSink`` over the Graph gateway slots in here and
                         ``ALERT_EMAIL_TO`` becomes the recipient (docs/operator/review-queue.md).

A sink failure never fails the digest: each sink's outcome is reported and the activity logs it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from wholesaling.api.deps import TEMPLATES_DIR, build_templates
from wholesaling.config.settings import Settings
from wholesaling.logging import get_logger
from wholesaling.reporting.kpis import KpiReport

log = get_logger(__name__)

SLACK_TIMEOUT_SECONDS = 10.0
SLACK_TEXT_LIMIT = 3000  # Slack truncates longer section text


@dataclass(slots=True)
class RenderedDigest:
    subject: str
    text: str
    html: str
    report: KpiReport


@dataclass(slots=True)
class SinkOutcome:
    sink: str
    ok: bool
    detail: str | None = None


@runtime_checkable
class DigestSink(Protocol):
    name: str

    async def send(self, digest: RenderedDigest) -> None: ...


class LogSink:
    """Default sink: the digest is a structured log event (and the text body at INFO)."""

    name = "log"

    def __init__(self) -> None:
        self.sent: list[RenderedDigest] = []

    async def send(self, digest: RenderedDigest) -> None:
        self.sent.append(digest)
        log.info(
            "digest.sent",
            sink=self.name,
            subject=digest.subject,
            kpis=digest.report.as_dict(),
            text=digest.text,
        )


class SlackWebhookSink:
    """POST the plain-text digest to a Slack incoming webhook (INT-18)."""

    name = "slack"

    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = SLACK_TIMEOUT_SECONDS,
    ) -> None:
        if not webhook_url.startswith("https://"):
            msg = "SLACK_WEBHOOK_URL must be an https URL"
            raise ValueError(msg)
        self._url = webhook_url
        self._client = client
        self._timeout = timeout

    @staticmethod
    def payload(digest: RenderedDigest) -> dict[str, Any]:
        body = digest.text
        if len(body) > SLACK_TEXT_LIMIT:
            body = body[: SLACK_TEXT_LIMIT - 20] + "\n... (truncated)"
        return {
            "text": digest.subject,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": digest.subject[:150]}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{body}```"}},
            ],
        }

    async def send(self, digest: RenderedDigest) -> None:
        payload = self.payload(digest)
        if self._client is not None:
            response = await self._client.post(self._url, json=payload, timeout=self._timeout)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json=payload)
        response.raise_for_status()
        log.info("digest.sent", sink=self.name, status=response.status_code)


def build_sinks(settings: Settings, *, client: httpx.AsyncClient | None = None) -> list[DigestSink]:
    """The sinks implied by settings: always the log, Slack when a webhook URL is configured."""
    sinks: list[DigestSink] = [LogSink()]
    if settings.slack_webhook_url is not None:
        url = settings.slack_webhook_url.get_secret_value().strip()
        if url:
            sinks.append(SlackWebhookSink(url, client=client))
    return sinks


# --------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------


def _env() -> Environment:
    # Reuse the API's filters (age / pct / money / dt) so the page and the digest agree.
    api_env = build_templates().env
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=False),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(api_env.filters)
    return env


def digest_subject(report: KpiReport) -> str:
    return (
        f"Wholesaling digest {report.day.isoformat()}: "
        f"{report.queue.depth} awaiting review, "
        f"{report.today.messages_sent} sent, {report.today.responses} replies"
    )


def render_digest(report: KpiReport) -> RenderedDigest:
    env = _env()
    context = {"report": report, "today": report.today, "week": report.rolling_7d}
    text = env.get_template("digest/digest.txt.j2").render(**context)
    html = env.get_template("digest/digest.html").render(**context)
    return RenderedDigest(subject=digest_subject(report), text=text, html=html, report=report)


@dataclass(slots=True)
class DigestDelivery:
    digest: RenderedDigest
    outcomes: list[SinkOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.ok for o in self.outcomes)


async def send_digest(report: KpiReport, sinks: list[DigestSink]) -> DigestDelivery:
    """Render once and deliver to every sink; a failing sink is reported, not raised."""
    digest = render_digest(report)
    delivery = DigestDelivery(digest=digest)
    for sink in sinks:
        try:
            await sink.send(digest)
        except Exception as exc:
            log.error("digest.sink_failed", sink=sink.name, error=str(exc)[:300])
            delivery.outcomes.append(SinkOutcome(sink=sink.name, ok=False, detail=str(exc)[:300]))
        else:
            delivery.outcomes.append(SinkOutcome(sink=sink.name, ok=True))
    return delivery


__all__ = [
    "DigestDelivery",
    "DigestSink",
    "LogSink",
    "RenderedDigest",
    "SinkOutcome",
    "SlackWebhookSink",
    "build_sinks",
    "digest_subject",
    "render_digest",
    "send_digest",
]
