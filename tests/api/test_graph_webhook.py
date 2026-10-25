"""POST /webhooks/graph: handshake, clientState verification, dedupe, enqueue (INT-12, XC-03)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from wholesaling.api.main import create_app
from wholesaling.api.routes.graph_webhook import NotificationDeduper, get_deduper
from wholesaling.config.settings import Settings
from wholesaling.integrations.temporal.mock import MockTemporalGateway
from wholesaling.workflows.graph_maintenance import InboundMessageInput, inbound_workflow_id

CLIENT_STATE = "a-very-random-client-state"


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "temporal_mode": "mock",
        "graph_mode": "mock",
        "graph_webhook_client_state": CLIENT_STATE,
        "graph_webhook_url": "https://api.example/webhooks/graph",
    }
    base.update(over)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app(_settings())
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _temporal(app: FastAPI) -> MockTemporalGateway:
    gw = app.state.temporal
    assert isinstance(gw, MockTemporalGateway)
    return gw


def _notification(
    resource_id: str = "AAMk1", subscription_id: str = "sub-1", client_state: str = CLIENT_STATE
) -> dict[str, Any]:
    return {
        "subscriptionId": subscription_id,
        "clientState": client_state,
        "changeType": "created",
        "resource": f"Users/mbx/Messages/{resource_id}",
        "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": resource_id},
        "subscriptionExpirationDateTime": "2026-10-28T00:00:00.000Z",
        "tenantId": "tenant-1",
    }


async def test_validation_handshake_echoes_decoded_token_as_text_plain(client: AsyncClient) -> None:
    r = await client.post("/webhooks/graph?validationToken=Validation%3ATest%20Token%2042")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text == "Validation:Test Token 42"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_handshake_wins_even_with_a_body(client: AsyncClient, app: FastAPI) -> None:
    r = await client.post("/webhooks/graph?validationToken=abc", json={"value": [_notification()]})
    assert r.status_code == 200 and r.text == "abc"
    assert _temporal(app).workflows == {}


async def test_notification_is_accepted_and_starts_inbound_workflow(
    client: AsyncClient, app: FastAPI
) -> None:
    r = await client.post("/webhooks/graph", json={"value": [_notification("AAMk1")]})
    assert r.status_code == 202
    assert r.json() == {"accepted": 1, "duplicates": 0, "lifecycle": 0, "failed": 0}
    temporal = _temporal(app)
    starts = [c for c in temporal.calls if c.method == "start_workflow"]
    assert len(starts) == 1
    assert starts[0].args["workflow"] == "GraphInboundMessageWorkflow"
    assert starts[0].args["workflow_id"] == inbound_workflow_id("sub-1", "AAMk1")
    assert starts[0].args["args"] == [
        InboundMessageInput(graph_id="AAMk1", subscription_id="sub-1", change_type="created")
    ]


async def test_duplicate_notification_is_a_no_op(client: AsyncClient, app: FastAPI) -> None:
    body = {"value": [_notification("AAMk1")]}
    first = await client.post("/webhooks/graph", json=body)
    second = await client.post("/webhooks/graph", json=body)
    assert first.status_code == second.status_code == 202
    assert second.json()["duplicates"] == 1 and second.json()["accepted"] == 0
    starts = [c for c in _temporal(app).calls if c.method == "start_workflow"]
    assert len(starts) == 1, "XC-03: repeated notification starts nothing"


async def test_duplicate_within_one_batch_counts_once(client: AsyncClient, app: FastAPI) -> None:
    r = await client.post(
        "/webhooks/graph", json={"value": [_notification("AAMk1"), _notification("AAMk1")]}
    )
    assert r.json() == {"accepted": 1, "duplicates": 1, "lifecycle": 0, "failed": 0}


async def test_same_message_under_another_subscription_is_distinct(
    client: AsyncClient, app: FastAPI
) -> None:
    await client.post("/webhooks/graph", json={"value": [_notification("AAMk1", "sub-1")]})
    r = await client.post("/webhooks/graph", json={"value": [_notification("AAMk1", "sub-2")]})
    assert r.json()["accepted"] == 1
    assert len(_temporal(app).workflows) == 2


async def test_dedupe_survives_cache_eviction_via_temporal_id_policy(
    client: AsyncClient, app: FastAPI
) -> None:
    """If the in-memory cache forgets a key, Temporal's id policy still makes it a no-op."""
    await client.post("/webhooks/graph", json={"value": [_notification("AAMk1")]})
    app.state.graph_dedupe = NotificationDeduper()  # simulate a restart / eviction
    r = await client.post("/webhooks/graph", json={"value": [_notification("AAMk1")]})
    assert r.status_code == 202
    temporal = _temporal(app)
    starts = [c for c in temporal.calls if c.method == "start_workflow"]
    assert len(starts) == 2, "the start was attempted again"
    assert len(temporal.workflows) == 1, "but the same workflow id is reused, not a second run"


async def test_client_state_mismatch_is_rejected_and_nothing_enqueued(
    client: AsyncClient, app: FastAPI
) -> None:
    r = await client.post(
        "/webhooks/graph",
        json={"value": [_notification("AAMk1"), _notification("AAMk2", client_state="wrong")]},
    )
    assert r.status_code == 401
    assert _temporal(app).workflows == {}


async def test_missing_client_state_is_rejected(client: AsyncClient, app: FastAPI) -> None:
    note = _notification("AAMk1")
    del note["clientState"]
    r = await client.post("/webhooks/graph", json={"value": [note]})
    assert r.status_code == 401
    assert _temporal(app).workflows == {}


async def test_unconfigured_client_state_fails_closed() -> None:
    application = create_app(_settings(graph_webhook_client_state=None))
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as c:
            handshake = await c.post("/webhooks/graph?validationToken=t")
            assert handshake.status_code == 200, (
                "handshake still works so subscriptions can be made"
            )
            r = await c.post("/webhooks/graph", json={"value": [_notification("AAMk1")]})
            assert r.status_code == 401
        assert _temporal(application).workflows == {}


async def test_malformed_payload_is_400(client: AsyncClient) -> None:
    r = await client.post(
        "/webhooks/graph", content=b"not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    r2 = await client.post("/webhooks/graph", json={"value": [{"noSubscriptionId": True}]})
    assert r2.status_code == 400


async def test_empty_batch_is_accepted(client: AsyncClient) -> None:
    r = await client.post("/webhooks/graph", json={"value": []})
    assert r.status_code == 202
    assert r.json()["accepted"] == 0


async def test_notification_without_resource_id_is_skipped(
    client: AsyncClient, app: FastAPI
) -> None:
    note = _notification("AAMk1")
    note["resourceData"] = {}
    note["resource"] = ""
    r = await client.post("/webhooks/graph", json={"value": [note]})
    assert r.status_code == 202 and r.json()["accepted"] == 0
    assert _temporal(app).workflows == {}


async def test_lifecycle_notification_starts_renewal(client: AsyncClient, app: FastAPI) -> None:
    r = await client.post(
        "/webhooks/graph",
        json={
            "value": [
                {
                    "subscriptionId": "sub-1",
                    "clientState": CLIENT_STATE,
                    "lifecycleEvent": "reauthorizationRequired",
                    "subscriptionExpirationDateTime": "2026-10-26T00:00:00Z",
                    "tenantId": "t",
                }
            ]
        },
    )
    assert r.status_code == 202
    assert r.json()["lifecycle"] == 1
    starts = [c for c in _temporal(app).calls if c.method == "start_workflow"]
    assert len(starts) == 1
    assert starts[0].args["workflow"] == "GraphSubscriptionRenewalWorkflow"
    assert starts[0].args["workflow_id"] == "graph-renewal-lifecycle-sub-1-reauthorizationRequired"


async def test_enqueue_failure_returns_503_so_graph_retries(
    client: AsyncClient, app: FastAPI
) -> None:
    temporal = _temporal(app)

    async def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("temporal down")

    temporal.start_workflow = broken  # type: ignore[method-assign]
    r = await client.post("/webhooks/graph", json={"value": [_notification("AAMk1")]})
    assert r.status_code == 503
    assert r.json()["failed"] == 1
    # Not remembered as seen: a retry from Graph must be attempted again.
    assert len(get_deduper_for(app)) == 0


def get_deduper_for(app: FastAPI) -> NotificationDeduper:
    class _Req:
        def __init__(self, application: FastAPI) -> None:
            self.app = application

    return get_deduper(_Req(app))  # type: ignore[arg-type]


async def test_already_started_error_counts_as_duplicate(client: AsyncClient, app: FastAPI) -> None:
    temporal = _temporal(app)

    class WorkflowAlreadyStartedError(Exception):
        pass

    async def already(*_a: Any, **_k: Any) -> None:
        raise WorkflowAlreadyStartedError("Workflow execution already started")

    temporal.start_workflow = already  # type: ignore[method-assign]
    r = await client.post("/webhooks/graph", json={"value": [_notification("AAMk7")]})
    assert r.status_code == 202
    assert r.json() == {"accepted": 0, "duplicates": 1, "lifecycle": 0, "failed": 0}


def test_deduper_evicts_oldest_at_capacity() -> None:
    d = NotificationDeduper(capacity=2)
    d.add(("s", "1"))
    d.add(("s", "2"))
    d.add(("s", "3"))
    assert not d.seen(("s", "1"))
    assert d.seen(("s", "2")) and d.seen(("s", "3"))
    assert len(d) == 2


def test_webhook_route_is_mounted_in_openapi() -> None:
    application = create_app(_settings())
    assert "/webhooks/graph" in application.openapi()["paths"]
