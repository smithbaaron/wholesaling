"""Live Microsoft Graph gateway over ``httpx`` (REQUIREMENTS.md INT-10..13, XC-08).

Resilience:

- retries with exponential backoff on 429 and 5xx, honouring ``Retry-After`` (seconds or
  HTTP-date); transport errors retry the same way;
- one transparent token refresh on 401;
- a circuit breaker: after ``failure_threshold`` consecutive failed requests the breaker opens
  for ``open_seconds`` and every call fails fast with ``GraphCircuitOpen``; the first call
  after the window is a half-open probe. ``healthcheck`` reports the breaker state.

Safety:

- ``send_draft`` refuses unless ``GRAPH_ALLOW_LIVE_SEND=true`` (RUL-06: no live messages in
  this build). Drafts can still be created and inspected in the mailbox.
- Tokens never reach logs. Request logs carry method, path, status and attempt only.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from wholesaling.config.settings import Settings
from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.graph.auth import ClientCredentialsTokenProvider
from wholesaling.integrations.graph.interface import (
    DEFAULT_FOLDER,
    GraphAuthError,
    GraphCircuitOpen,
    GraphError,
    GraphNotFound,
    GraphSendForbidden,
    GraphThrottled,
)
from wholesaling.integrations.graph.models import (
    DeltaPage,
    DraftInfo,
    InboundMessage,
    OutboundDraft,
    SendResult,
    Subscription,
)
from wholesaling.logging import get_logger

log = get_logger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# Properties fetched for a message. ``internetMessageHeaders`` is requested on single-message
# GETs only; delta responses may omit it, and the model tolerates that.
MESSAGE_SELECT = (
    "id,internetMessageId,conversationId,subject,from,sender,toRecipients,ccRecipients,"
    "receivedDateTime,sentDateTime,isDraft,isRead,bodyPreview,body"
)
MESSAGE_SELECT_WITH_HEADERS = MESSAGE_SELECT + ",internetMessageHeaders"
# Bound one delta round so a runaway mailbox cannot pin an activity forever.
MAX_DELTA_PAGES = 50
DELTA_PAGE_SIZE = 50

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class CircuitBreaker:
    """Consecutive-failure breaker with an open window (XC-08)."""

    def __init__(
        self, *, failure_threshold: int = 5, open_seconds: float = 60.0, clock: Clock | None = None
    ) -> None:
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._clock: Clock = clock or _utcnow
        self.failures = 0
        self.opened_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        elapsed = (self._clock() - self.opened_at).total_seconds()
        return "half_open" if elapsed >= self.open_seconds else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.last_error = None

    def record_failure(self, error: str) -> None:
        self.failures += 1
        self.last_error = error
        if self.failures >= self.failure_threshold:
            if self.opened_at is None or self.state == "half_open":
                log.warning(
                    "graph.circuit_opened", failures=self.failures, open_seconds=self.open_seconds
                )
            self.opened_at = self._clock()

    def describe(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "failures": self.failures,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "last_error": self.last_error,
        }


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """``Retry-After`` as seconds. Accepts delta-seconds or an HTTP-date."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class LiveGraphGateway:
    name = "graph"
    mode: IntegrationMode = "live"

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        token_provider: ClientCredentialsTokenProvider | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        max_retries: int = 3,
        base_backoff: float = 0.5,
        max_backoff: float = 30.0,
        breaker: CircuitBreaker | None = None,
        base_url: str = GRAPH_BASE_URL,
    ) -> None:
        if not settings.graph_mailbox:
            raise GraphAuthError("GRAPH_MAILBOX is required in live mode")
        self._settings = settings
        self.mailbox = settings.graph_mailbox
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_http = http is None
        self._clock: Clock = clock or _utcnow
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._base_url = base_url.rstrip("/")
        self.breaker = breaker or CircuitBreaker(clock=self._clock)
        self._tokens = token_provider or ClientCredentialsTokenProvider(
            tenant_id=settings.graph_tenant_id or "",
            client_id=settings.graph_client_id or "",
            client_secret=(
                settings.graph_client_secret.get_secret_value()
                if settings.graph_client_secret
                else ""
            ),
            http=self._http,
            clock=self._clock,
        )
        self.request_count = 0

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- paths -------------------------------------------------------------------------------

    @property
    def mailbox_path(self) -> str:
        return f"/users/{quote(self.mailbox, safe='@')}"

    def subscription_resource(self, folder: str = DEFAULT_FOLDER) -> str:
        """The ``resource`` string Graph expects for inbox message notifications."""
        return f"users/{self.mailbox}/mailFolders('{folder}')/messages"

    # -- HTTP core ---------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> httpx.Response:
        if not self.breaker.allow():
            raise GraphCircuitOpen(
                f"graph circuit open since {self.breaker.opened_at}; last error: "
                f"{self.breaker.last_error}"
            )
        url = path_or_url if path_or_url.startswith("http") else f"{self._base_url}{path_or_url}"
        refreshed_token = False
        attempt = 0
        while True:
            attempt += 1
            token = await self._tokens.get_token()
            req_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if headers:
                req_headers.update(headers)
            try:
                self.request_count += 1
                response = await self._http.request(
                    method, url, json=json, params=params, headers=req_headers
                )
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt <= self._max_retries:
                    await self._backoff(attempt, None, reason=error)
                    continue
                self.breaker.record_failure(error)
                raise GraphError(f"graph request failed after {attempt} attempts: {error}") from exc

            status = response.status_code
            log.debug("graph.request", method=method, url=url, status=status, attempt=attempt)
            if status in expected:
                self.breaker.record_success()
                return response
            if status == 401 and not refreshed_token:
                self._tokens.invalidate()
                refreshed_token = True
                continue
            if status in RETRYABLE_STATUS:
                retry_after = parse_retry_after(response.headers.get("Retry-After"), self._clock())
                if attempt <= self._max_retries:
                    await self._backoff(attempt, retry_after, reason=f"HTTP {status}")
                    continue
                self.breaker.record_failure(f"HTTP {status}")
                if status == 429:
                    raise GraphThrottled(
                        "graph throttled; retry budget exhausted",
                        status_code=status,
                        retry_after=retry_after,
                    )
                raise GraphError(f"graph server error after {attempt} attempts", status_code=status)
            # Non-retryable client error: not a backend fault, so it does not trip the breaker.
            code, message = _error_body(response)
            if status == 404:
                raise GraphNotFound(message or "not found", status_code=404, code=code)
            if status in (401, 403):
                raise GraphAuthError(message or "forbidden", status_code=status, code=code)
            raise GraphError(message or f"HTTP {status}", status_code=status, code=code)

    async def _backoff(self, attempt: int, retry_after: float | None, *, reason: str) -> None:
        if retry_after is not None:
            delay = min(retry_after, self._max_backoff)
        else:
            delay = min(self._base_backoff * (2 ** (attempt - 1)), self._max_backoff)
            delay += random.uniform(0, delay * 0.1)
        log.info("graph.retry", attempt=attempt, delay_seconds=round(delay, 3), reason=reason)
        await self._sleep(delay)

    # -- GraphGateway ------------------------------------------------------------------------

    async def healthcheck(self) -> HealthStatus:
        breaker = self.breaker.describe()
        if breaker["state"] == "open":
            return HealthStatus(
                name=self.name, mode=self.mode, ok=False, detail=f"circuit open: {breaker}"
            )
        try:
            await self._request(
                "GET", f"{self.mailbox_path}/mailFolders/{DEFAULT_FOLDER}", params={"$select": "id"}
            )
        except Exception as exc:  # health must never raise
            return HealthStatus(name=self.name, mode=self.mode, ok=False, detail=str(exc))
        return HealthStatus(
            name=self.name, mode=self.mode, ok=True, detail=f"circuit {breaker['state']}"
        )

    async def create_subscription(
        self,
        *,
        resource: str,
        notification_url: str,
        client_state: str,
        expires_at: datetime,
        change_types: list[str] | None = None,
        lifecycle_notification_url: str | None = None,
    ) -> Subscription:
        body: dict[str, Any] = {
            "changeType": ",".join(change_types or ["created"]),
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": _iso(expires_at),
            "clientState": client_state,
        }
        if lifecycle_notification_url:
            body["lifecycleNotificationUrl"] = lifecycle_notification_url
        response = await self._request("POST", "/subscriptions", json=body, expected=(201, 200))
        sub = Subscription.from_graph(response.json())
        log.info(
            "graph.subscription_created", subscription_id=sub.id, expires_at=_iso(sub.expires_at)
        )
        return sub

    async def renew_subscription(self, subscription_id: str, expires_at: datetime) -> Subscription:
        response = await self._request(
            "PATCH",
            f"/subscriptions/{quote(subscription_id)}",
            json={"expirationDateTime": _iso(expires_at)},
        )
        sub = Subscription.from_graph(response.json())
        log.info(
            "graph.subscription_renewed", subscription_id=sub.id, expires_at=_iso(sub.expires_at)
        )
        return sub

    async def list_subscriptions(self) -> list[Subscription]:
        subs: list[Subscription] = []
        url: str | None = "/subscriptions"
        while url:
            response = await self._request("GET", url)
            payload = response.json()
            subs.extend(Subscription.from_graph(item) for item in payload.get("value", []))
            url = payload.get("@odata.nextLink")
        return subs

    async def delete_subscription(self, subscription_id: str) -> None:
        await self._request("DELETE", f"/subscriptions/{quote(subscription_id)}", expected=(204,))

    async def get_message(self, graph_id: str) -> InboundMessage:
        response = await self._request(
            "GET",
            f"{self.mailbox_path}/messages/{quote(graph_id, safe='')}",
            params={"$select": MESSAGE_SELECT_WITH_HEADERS},
            headers={"Prefer": 'outlook.body-content-type="text"'},
        )
        return InboundMessage.from_graph(response.json(), own_mailbox=self.mailbox)

    async def delta_messages(
        self,
        folder: str = DEFAULT_FOLDER,
        delta_token: str | None = None,
        *,
        since: datetime | None = None,
    ) -> DeltaPage:
        messages: list[InboundMessage] = []
        removed: list[str] = []
        prefer = f'odata.maxpagesize={DELTA_PAGE_SIZE}, outlook.body-content-type="text"'
        if delta_token:
            # The token is the whole @odata.deltaLink (opaque, carries $deltatoken and $select).
            url: str | None = delta_token
            params: dict[str, str] | None = None
        else:
            url = f"{self.mailbox_path}/mailFolders/{quote(folder)}/messages/delta"
            params = {"$select": MESSAGE_SELECT}
            if since is not None:
                params["$filter"] = f"receivedDateTime ge {_iso(since)}"
        pages = 0
        next_token: str | None = None
        while url and pages < MAX_DELTA_PAGES:
            response = await self._request("GET", url, params=params, headers={"Prefer": prefer})
            params = None
            pages += 1
            payload = response.json()
            for item in payload.get("value", []):
                if "@removed" in item:
                    removed.append(str(item.get("id")))
                    continue
                messages.append(InboundMessage.from_graph(item, own_mailbox=self.mailbox))
            next_link = payload.get("@odata.nextLink")
            if next_link:
                url = next_link
                continue
            next_token = payload.get("@odata.deltaLink")
            url = None
        if url is not None:
            # Page cap hit: hand back the nextLink so the next round resumes mid-stream.
            next_token = url
        return DeltaPage(
            messages=messages, removed_ids=removed, next_delta_token=next_token, pages_fetched=pages
        )

    async def create_draft(self, draft: OutboundDraft) -> DraftInfo:
        if draft.reply_to_graph_message_id is not None:
            return await self.create_reply_draft(draft.reply_to_graph_message_id, draft.body_text)
        if draft.conversation_id is not None:
            latest = await self._latest_message_in_conversation(draft.conversation_id)
            if latest is not None:
                return await self.create_reply_draft(latest, draft.body_text)
        body: dict[str, Any] = {
            "subject": draft.subject,
            "body": (
                {"contentType": "HTML", "content": draft.body_html}
                if draft.body_html
                else {"contentType": "Text", "content": draft.body_text}
            ),
            "toRecipients": [_recipient(a) for a in draft.to],
            "isDraft": True,
        }
        if draft.cc:
            body["ccRecipients"] = [_recipient(a) for a in draft.cc]
        response = await self._request(
            "POST", f"{self.mailbox_path}/messages", json=body, expected=(201,)
        )
        payload = response.json()
        info = DraftInfo(
            draft_id=str(payload["id"]),
            internet_message_id=payload.get("internetMessageId"),
            conversation_id=payload.get("conversationId"),
            raw=payload,
        )
        log.info("graph.draft_created", draft_id=info.draft_id, reply=False)
        return info

    async def create_reply_draft(self, graph_message_id: str, body: str) -> DraftInfo:
        # ``createReply`` with ``comment`` keeps the quoted original and the conversation
        # threading headers; the draft is saved, not sent (INT-13).
        response = await self._request(
            "POST",
            f"{self.mailbox_path}/messages/{quote(graph_message_id, safe='')}/createReply",
            json={"comment": body},
            expected=(201, 200),
        )
        payload = response.json()
        info = DraftInfo(
            draft_id=str(payload["id"]),
            internet_message_id=payload.get("internetMessageId"),
            conversation_id=payload.get("conversationId"),
            raw=payload,
        )
        log.info("graph.draft_created", draft_id=info.draft_id, reply=True)
        return info

    async def send_draft(self, draft_id: str) -> SendResult:
        if not self._settings.graph_allow_live_send:
            raise GraphSendForbidden(
                "live sending is disabled (GRAPH_ALLOW_LIVE_SEND=false; REQUIREMENTS.md RUL-06)"
            )
        # Read ids before sending: once sent the item moves to Sent Items under a new id.
        internet_id: str | None = None
        conversation_id: str | None = None
        try:
            pre = await self._request(
                "GET",
                f"{self.mailbox_path}/messages/{quote(draft_id, safe='')}",
                params={"$select": "id,internetMessageId,conversationId,isDraft"},
            )
            pre_payload = pre.json()
            internet_id = pre_payload.get("internetMessageId")
            conversation_id = pre_payload.get("conversationId")
            if pre_payload.get("isDraft") is False:
                raise GraphNotFound(f"message {draft_id} is not a draft (already sent)")
        except GraphNotFound:
            raise
        except GraphError as exc:
            log.warning("graph.send_precheck_failed", draft_id=draft_id, error=str(exc))
        await self._request(
            "POST",
            f"{self.mailbox_path}/messages/{quote(draft_id, safe='')}/send",
            expected=(202,),
        )
        now = self._clock()
        log.info("graph.draft_sent", draft_id=draft_id, sent_at=_iso(now))
        return SendResult(
            draft_id=draft_id,
            sent=True,
            sent_at=now,
            graph_message_id=draft_id,
            internet_message_id=internet_id,
            conversation_id=conversation_id,
        )

    # -- internals ---------------------------------------------------------------------------

    async def _latest_message_in_conversation(self, conversation_id: str) -> str | None:
        # Graph requires every $orderby property to appear in $filter first.
        safe_conv = conversation_id.replace("'", "''")
        response = await self._request(
            "GET",
            f"{self.mailbox_path}/messages",
            params={
                "$filter": (
                    f"conversationId eq '{safe_conv}' and receivedDateTime ge 1970-01-01T00:00:00Z"
                ),
                "$orderby": "receivedDateTime desc",
                "$top": "1",
                "$select": "id,conversationId,receivedDateTime",
            },
        )
        items = response.json().get("value", [])
        return str(items[0]["id"]) if items else None


def _recipient(address: str) -> dict[str, Any]:
    return {"emailAddress": {"address": address}}


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _error_body(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        body = response.json()
    except ValueError:
        return None, None
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return None, None
    return err.get("code"), err.get("message")


__all__ = [
    "GRAPH_BASE_URL",
    "CircuitBreaker",
    "LiveGraphGateway",
    "parse_retry_after",
]
