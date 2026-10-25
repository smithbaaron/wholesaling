"""Live Graph client against ``httpx.MockTransport``: no network, no credentials.

Covers token caching (INT-11), retry/backoff honouring Retry-After, the circuit breaker
(XC-08), 401 refresh, error mapping, delta paging, draft/reply/send calls and the RUL-06
live-send guard.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.graph import (
    GraphAuthError,
    GraphCircuitOpen,
    GraphError,
    GraphNotFound,
    GraphSendForbidden,
    GraphThrottled,
    OutboundDraft,
    get_graph,
)
from wholesaling.integrations.graph.auth import AccessToken, ClientCredentialsTokenProvider
from wholesaling.integrations.graph.live import (
    GRAPH_BASE_URL,
    CircuitBreaker,
    LiveGraphGateway,
    parse_retry_after,
)

TOKEN_URL = "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
MAILBOX = "deals@northstack-deals.com"


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Recorder:
    """Scripted responses keyed by (method, path); records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.scripts: dict[tuple[str, str], list[Callable[[httpx.Request], httpx.Response]]] = {}
        self.token_calls = 0
        self.token_expires_in = 3600

    def on(
        self, method: str, path: str, *responses: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self.scripts.setdefault((method.upper(), path), []).extend(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            body = request.content.decode()
            assert "grant_type=client_credentials" in body
            assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_in": self.token_expires_in,
                    "access_token": f"tok-{self.token_calls}",
                },
            )
        key = (request.method, request.url.path)
        queue = self.scripts.get(key)
        if not queue:
            return httpx.Response(500, json={"error": {"code": "unscripted", "message": str(key)}})
        fn = queue.pop(0) if len(queue) > 1 else queue[0]
        return fn(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def ok(payload: dict[str, Any], status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(status, json=payload)


def status(
    code: int, headers: dict[str, str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(
        code, headers=headers or {}, json={"error": {"code": "x", "message": f"HTTP {code}"}}
    )


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "graph_mode": "live",
        "graph_tenant_id": "tenant-1",
        "graph_client_id": "client-1",
        "graph_client_secret": "shh",
        "graph_mailbox": MAILBOX,
        "graph_webhook_url": "https://api.example/webhooks/graph",
        "graph_webhook_client_state": "state-secret",
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def gateway(rec: Recorder, clock: FakeClock, sleeps: list[float]) -> LiveGraphGateway:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    http = httpx.AsyncClient(transport=rec.transport())
    return LiveGraphGateway(
        _settings(),
        http=http,
        clock=clock,
        sleeper=fake_sleep,
        max_retries=2,
        base_backoff=0.1,
        breaker=CircuitBreaker(failure_threshold=2, open_seconds=60, clock=clock),
    )


def _mailbox_path() -> str:
    return f"/v1.0/users/{MAILBOX}"


# -- auth ------------------------------------------------------------------------------------


async def test_token_is_cached_until_near_expiry(rec: Recorder, clock: FakeClock) -> None:
    http = httpx.AsyncClient(transport=rec.transport())
    provider = ClientCredentialsTokenProvider(
        tenant_id="tenant-1", client_id="client-1", client_secret="shh", http=http, clock=clock
    )
    assert await provider.get_token() == "tok-1"
    assert await provider.get_token() == "tok-1"
    assert rec.token_calls == 1
    clock.advance(3600 - 299)  # inside the 300 s refresh skew
    assert await provider.get_token() == "tok-2"
    assert rec.token_calls == 2
    provider.invalidate()
    assert await provider.get_token() == "tok-3"


async def test_token_error_is_graph_auth_error_without_leaking_secret(rec: Recorder) -> None:
    def bad(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": "invalid_client",
                "error_description": "AADSTS7000215: bad secret\nTrace",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(bad))
    provider = ClientCredentialsTokenProvider(
        tenant_id="t", client_id="c", client_secret="super-secret-value", http=http
    )
    with pytest.raises(GraphAuthError) as exc:
        await provider.get_token()
    assert "invalid_client" in str(exc.value)
    assert "super-secret-value" not in str(exc.value)
    assert "Trace" not in str(exc.value)


def test_access_token_repr_hides_value() -> None:
    tok = AccessToken(value="very-secret", expires_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert "very-secret" not in repr(tok)


def test_provider_requires_all_three_credentials() -> None:
    with pytest.raises(GraphAuthError):
        ClientCredentialsTokenProvider(
            tenant_id="t", client_id="", client_secret="s", http=httpx.AsyncClient()
        )


def test_factory_builds_live_gateway_and_requires_mailbox() -> None:
    gw = get_graph(_settings())
    assert isinstance(gw, LiveGraphGateway) and gw.mode == "live"
    with pytest.raises(GraphAuthError):
        get_graph(_settings(graph_mailbox=None))


# -- requests, retries, breaker ----------------------------------------------------------------


async def test_get_message_sends_bearer_and_parses(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on(
        "GET",
        f"{_mailbox_path()}/messages/AAMk1",
        ok(
            {
                "id": "AAMk1",
                "internetMessageId": "<m1@x>",
                "conversationId": "C1",
                "from": {"emailAddress": {"address": "jane@acme.com"}},
                "body": {"contentType": "text", "content": "hello"},
                "receivedDateTime": "2026-10-25T10:00:00Z",
            }
        ),
    )
    msg = await gateway.get_message("AAMk1")
    assert msg.external_message_id == "<m1@x>" and msg.conversation_id == "C1"
    req = rec.requests[-1]
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert "internetMessageHeaders" in req.url.params["$select"]
    assert req.headers["Prefer"] == 'outlook.body-content-type="text"'


async def test_retries_on_429_honouring_retry_after(
    gateway: LiveGraphGateway, rec: Recorder, sleeps: list[float]
) -> None:
    rec.on(
        "GET",
        f"{_mailbox_path()}/messages/AAMk2",
        status(429, {"Retry-After": "7"}),
        ok({"id": "AAMk2", "internetMessageId": "<m2@x>"}),
    )
    msg = await gateway.get_message("AAMk2")
    assert msg.graph_id == "AAMk2"
    assert sleeps == [7.0]
    assert gateway.breaker.failures == 0


async def test_retries_5xx_with_backoff_then_gives_up(
    gateway: LiveGraphGateway, rec: Recorder, sleeps: list[float]
) -> None:
    rec.on("GET", f"{_mailbox_path()}/messages/AAMk3", status(503))
    with pytest.raises(GraphError) as exc:
        await gateway.get_message("AAMk3")
    assert exc.value.status_code == 503
    assert len(sleeps) == 2, "max_retries=2 means two backoffs then failure"
    assert sleeps[1] > sleeps[0], "exponential"
    assert gateway.breaker.failures == 1


async def test_throttled_after_budget_raises_graph_throttled(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on("GET", f"{_mailbox_path()}/messages/AAMk4", status(429, {"Retry-After": "1"}))
    with pytest.raises(GraphThrottled) as exc:
        await gateway.get_message("AAMk4")
    assert exc.value.retry_after == 1.0


async def test_circuit_opens_after_threshold_and_half_opens_after_window(
    gateway: LiveGraphGateway, rec: Recorder, clock: FakeClock
) -> None:
    rec.on("GET", f"{_mailbox_path()}/messages/BAD", status(500))
    for _ in range(2):
        with pytest.raises(GraphError):
            await gateway.get_message("BAD")
    assert gateway.breaker.state == "open"
    requests_before = len(rec.requests)
    with pytest.raises(GraphCircuitOpen):
        await gateway.get_message("BAD")
    assert len(rec.requests) == requests_before, "open circuit makes no request"
    health = await gateway.healthcheck()
    assert health.ok is False and "circuit open" in health.detail

    clock.advance(61)
    assert gateway.breaker.state == "half_open"
    rec.on("GET", f"{_mailbox_path()}/mailFolders/inbox", ok({"id": "inbox"}))
    health = await gateway.healthcheck()
    assert health.ok is True and gateway.breaker.state == "closed"


async def test_401_triggers_one_token_refresh(gateway: LiveGraphGateway, rec: Recorder) -> None:
    rec.on(
        "GET",
        f"{_mailbox_path()}/messages/AAMk5",
        status(401),
        ok({"id": "AAMk5", "internetMessageId": "<m5@x>"}),
    )
    await gateway.get_message("AAMk5")
    auth_headers = [r.headers["Authorization"] for r in rec.requests if "/messages/" in r.url.path]
    assert auth_headers == ["Bearer tok-1", "Bearer tok-2"]
    assert rec.token_calls == 2


async def test_persistent_401_is_auth_error_not_retried_forever(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on("GET", f"{_mailbox_path()}/messages/AAMk6", status(401))
    with pytest.raises(GraphAuthError):
        await gateway.get_message("AAMk6")
    assert rec.token_calls == 2


async def test_404_maps_to_not_found_and_does_not_trip_breaker(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on("GET", f"{_mailbox_path()}/messages/GONE", status(404))
    with pytest.raises(GraphNotFound):
        await gateway.get_message("GONE")
    assert gateway.breaker.failures == 0


async def test_transport_errors_retry_then_fail(
    gateway: LiveGraphGateway, rec: Recorder, sleeps: list[float]
) -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    rec.on("GET", f"{_mailbox_path()}/messages/NET", boom)
    with pytest.raises(GraphError):
        await gateway.get_message("NET")
    assert len(sleeps) == 2
    assert gateway.breaker.failures == 1


def test_parse_retry_after_seconds_and_http_date() -> None:
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    assert parse_retry_after("12", now) == 12.0
    assert parse_retry_after("Sat, 25 Oct 2026 12:00:30 GMT", now) == 30.0
    assert parse_retry_after("garbage", now) is None
    assert parse_retry_after(None, now) is None


# -- subscriptions ---------------------------------------------------------------------------


async def test_create_renew_list_delete_subscription(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on(
        "POST",
        "/v1.0/subscriptions",
        ok(
            {
                "id": "sub-1",
                "resource": gateway.subscription_resource(),
                "changeType": "created",
                "expirationDateTime": "2026-10-28T10:00:00.000Z",
                "notificationUrl": "https://api.example/webhooks/graph",
            },
            201,
        ),
    )
    sub = await gateway.create_subscription(
        resource=gateway.subscription_resource(),
        notification_url="https://api.example/webhooks/graph",
        client_state="state-secret",
        expires_at=datetime(2026, 10, 28, 10, tzinfo=UTC),
        lifecycle_notification_url="https://api.example/webhooks/graph",
    )
    assert sub.id == "sub-1"
    body = json.loads(rec.requests[-1].content)
    assert body["clientState"] == "state-secret"
    assert body["expirationDateTime"] == "2026-10-28T10:00:00.000Z"
    assert body["resource"] == f"users/{MAILBOX}/mailFolders('inbox')/messages"
    assert body["lifecycleNotificationUrl"] == "https://api.example/webhooks/graph"

    rec.on(
        "PATCH",
        "/v1.0/subscriptions/sub-1",
        ok({"id": "sub-1", "resource": "r", "expirationDateTime": "2026-10-29T10:00:00Z"}),
    )
    renewed = await gateway.renew_subscription("sub-1", datetime(2026, 10, 29, 10, tzinfo=UTC))
    assert renewed.expires_at == datetime(2026, 10, 29, 10, tzinfo=UTC)

    rec.on(
        "GET",
        "/v1.0/subscriptions",
        ok(
            {
                "value": [
                    {"id": "sub-1", "resource": "r", "expirationDateTime": "2026-10-29T10:00:00Z"}
                ]
            }
        ),
    )
    assert [s.id for s in await gateway.list_subscriptions()] == ["sub-1"]

    rec.on("DELETE", "/v1.0/subscriptions/sub-1", lambda _r: httpx.Response(204))
    await gateway.delete_subscription("sub-1")
    assert rec.requests[-1].method == "DELETE"


# -- delta -----------------------------------------------------------------------------------


async def test_delta_follows_next_links_until_delta_link(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    delta_path = f"{_mailbox_path()}/mailFolders/inbox/messages/delta"
    page2_url = f"{GRAPH_BASE_URL}/users/{MAILBOX}/mailFolders/inbox/messages/delta?$skiptoken=p2"
    delta_link = f"{GRAPH_BASE_URL}/users/{MAILBOX}/mailFolders/inbox/messages/delta?$deltatoken=D1"

    def page(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("$skiptoken") == "p2":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "B", "internetMessageId": "<b@x>"},
                        {"id": "GONE", "@removed": {"reason": "deleted"}},
                    ],
                    "@odata.deltaLink": delta_link,
                },
            )
        assert request.url.params["$filter"].startswith("receivedDateTime ge 2026-10-24T12:00:00")
        return httpx.Response(
            200,
            json={
                "value": [{"id": "A", "internetMessageId": "<a@x>"}],
                "@odata.nextLink": page2_url,
            },
        )

    rec.on("GET", delta_path, page)
    result = await gateway.delta_messages(
        "inbox", None, since=datetime(2026, 10, 24, 12, tzinfo=UTC)
    )
    assert [m.graph_id for m in result.messages] == ["A", "B"]
    assert result.removed_ids == ["GONE"]
    assert result.next_delta_token == delta_link
    assert result.pages_fetched == 2
    assert all(
        "odata.maxpagesize" in r.headers.get("Prefer", "")
        for r in rec.requests
        if "delta" in r.url.path
    )

    # Next round uses the delta link verbatim (no $filter, no $select re-sent).
    def from_token(request: httpx.Request) -> httpx.Response:
        assert request.url.params["$deltatoken"] == "D1"
        assert "$filter" not in request.url.params
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": delta_link})

    rec.scripts.clear()
    rec.on("GET", delta_path, from_token)
    again = await gateway.delta_messages("inbox", delta_link)
    assert again.messages == [] and again.next_delta_token == delta_link


# -- drafts and sends ------------------------------------------------------------------------


async def test_create_draft_posts_message_and_never_sends(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on(
        "POST",
        f"{_mailbox_path()}/messages",
        ok({"id": "D1", "internetMessageId": "<d1@x>", "conversationId": "C9"}, 201),
    )
    info = await gateway.create_draft(
        OutboundDraft(to=["jane@acme.com"], cc=["ops@acme.com"], subject="S", body_text="B")
    )
    assert info.draft_id == "D1" and info.conversation_id == "C9"
    body = json.loads(rec.requests[-1].content)
    assert body["isDraft"] is True
    assert body["toRecipients"] == [{"emailAddress": {"address": "jane@acme.com"}}]
    assert body["ccRecipients"] == [{"emailAddress": {"address": "ops@acme.com"}}]
    assert body["body"] == {"contentType": "Text", "content": "B"}
    assert not any(r.url.path.endswith("/send") for r in rec.requests)


async def test_create_reply_draft_uses_create_reply_with_comment(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on(
        "POST",
        f"{_mailbox_path()}/messages/ORIG/createReply",
        ok({"id": "R1", "conversationId": "C1", "internetMessageId": "<r1@x>"}, 201),
    )
    info = await gateway.create_reply_draft("ORIG", "Thanks, here you go.")
    assert info.draft_id == "R1" and info.conversation_id == "C1"
    assert json.loads(rec.requests[-1].content) == {"comment": "Thanks, here you go."}


async def test_create_draft_with_conversation_id_replies_to_latest(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    rec.on("GET", f"{_mailbox_path()}/messages", ok({"value": [{"id": "LATEST"}]}))
    rec.on(
        "POST",
        f"{_mailbox_path()}/messages/LATEST/createReply",
        ok({"id": "R2", "conversationId": "C1"}, 201),
    )
    info = await gateway.create_draft(
        OutboundDraft(to=["jane@acme.com"], subject="S", body_text="B", conversation_id="C1")
    )
    assert info.draft_id == "R2"
    listing = next(
        r for r in rec.requests if r.method == "GET" and r.url.path.endswith("/messages")
    )
    assert "conversationId eq 'C1'" in listing.url.params["$filter"]
    assert listing.url.params["$orderby"] == "receivedDateTime desc"


async def test_send_draft_refused_unless_live_send_enabled(
    gateway: LiveGraphGateway, rec: Recorder
) -> None:
    with pytest.raises(GraphSendForbidden):
        await gateway.send_draft("D1")
    assert rec.requests == [], "no token fetched, no request made"


async def test_send_draft_when_enabled_reads_ids_then_posts_send(
    rec: Recorder, clock: FakeClock
) -> None:
    gw = LiveGraphGateway(
        _settings(graph_allow_live_send=True),
        http=httpx.AsyncClient(transport=rec.transport()),
        clock=clock,
    )
    rec.on(
        "GET",
        f"{_mailbox_path()}/messages/D1",
        ok({"id": "D1", "internetMessageId": "<d1@x>", "conversationId": "C1", "isDraft": True}),
    )
    rec.on("POST", f"{_mailbox_path()}/messages/D1/send", lambda _r: httpx.Response(202))
    result = await gw.send_draft("D1")
    assert result.sent and result.internet_message_id == "<d1@x>" and result.conversation_id == "C1"
    assert result.sent_at == clock.now
    assert [r.method for r in rec.requests if "/messages/D1" in r.url.path] == ["GET", "POST"]


async def test_send_draft_already_sent_is_not_found(rec: Recorder, clock: FakeClock) -> None:
    gw = LiveGraphGateway(
        _settings(graph_allow_live_send=True),
        http=httpx.AsyncClient(transport=rec.transport()),
        clock=clock,
    )
    rec.on("GET", f"{_mailbox_path()}/messages/D2", ok({"id": "D2", "isDraft": False}))
    with pytest.raises(GraphNotFound):
        await gw.send_draft("D2")
    assert not any(r.url.path.endswith("/send") for r in rec.requests)
