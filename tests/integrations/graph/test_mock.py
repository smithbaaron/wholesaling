"""MockGraphGateway behaviour the activities, webhook tests and synthetic pilot depend on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.graph import (
    GraphGateway,
    GraphNotFound,
    MockGraphGateway,
    OutboundDraft,
    get_graph,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def gw() -> MockGraphGateway:
    return MockGraphGateway(mailbox="deals@example.test", clock=lambda: NOW)


def test_factory_returns_mock_by_default() -> None:
    s = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    g = get_graph(s)
    assert isinstance(g, MockGraphGateway)
    assert isinstance(g, GraphGateway)
    assert g.mode == "mock"


async def test_healthcheck_reports_mode_and_forced_failure(gw: MockGraphGateway) -> None:
    ok = await gw.healthcheck()
    assert (ok.name, ok.mode, ok.ok) == ("graph", "mock", True)
    gw.healthy = False
    assert (await gw.healthcheck()).ok is False
    assert [c.method for c in gw.calls] == ["healthcheck", "healthcheck"]


async def test_inject_and_get_message(gw: MockGraphGateway) -> None:
    msg = gw.inject_inbound(from_address="jane@acme.com", subject="Hi", body="Interested.")
    fetched = await gw.get_message(msg.graph_id)
    assert fetched == msg
    assert fetched.external_message_id.startswith("<") and fetched.conversation_id
    assert fetched.received_at == NOW
    with pytest.raises(GraphNotFound):
        await gw.get_message("nope")


async def test_inject_runs_bounce_detection(gw: MockGraphGateway) -> None:
    msg = gw.inject_inbound(
        from_address="postmaster@outlook.com",
        subject="Undeliverable: Off-market deals",
        body="Delivery to the following recipient failed: dead@nowhere.com",
    )
    assert msg.is_bounce and msg.bounce.bounced_address == "dead@nowhere.com"


async def test_delta_cursor_returns_only_new_messages(gw: MockGraphGateway) -> None:
    a = gw.inject_inbound(from_address="a@x.com")
    page1 = await gw.delta_messages("inbox", None)
    assert [m.graph_id for m in page1.messages] == [a.graph_id]
    b = gw.inject_inbound(from_address="b@x.com")
    page2 = await gw.delta_messages("inbox", page1.next_delta_token)
    assert [m.graph_id for m in page2.messages] == [b.graph_id]
    page3 = await gw.delta_messages("inbox", page2.next_delta_token)
    assert page3.messages == []
    gw.inject_inbound(from_address="c@x.com", folder="sentitems")
    assert (await gw.delta_messages("inbox", page3.next_delta_token)).messages == []


async def test_delta_since_filters_initial_round_only(gw: MockGraphGateway) -> None:
    old = gw.inject_inbound(from_address="old@x.com", received_at=NOW - timedelta(days=3))
    new = gw.inject_inbound(from_address="new@x.com", received_at=NOW - timedelta(hours=1))
    page = await gw.delta_messages("inbox", None, since=NOW - timedelta(hours=24))
    assert [m.graph_id for m in page.messages] == [new.graph_id]
    full = await gw.delta_messages("inbox", None)
    assert {m.graph_id for m in full.messages} == {old.graph_id, new.graph_id}


async def test_delta_token_for_other_folder_is_rejected(gw: MockGraphGateway) -> None:
    page = await gw.delta_messages("inbox", None)
    with pytest.raises(GraphNotFound):
        await gw.delta_messages("sentitems", page.next_delta_token)


async def test_subscription_lifecycle_and_expiry(gw: MockGraphGateway) -> None:
    sub = await gw.create_subscription(
        resource="users/deals@example.test/mailFolders('inbox')/messages",
        notification_url="https://api/webhooks/graph",
        client_state="s3cret",
        expires_at=NOW + timedelta(hours=70),
    )
    assert [s.id for s in await gw.list_subscriptions()] == [sub.id]
    renewed = await gw.renew_subscription(sub.id, NOW + timedelta(hours=100))
    assert renewed.expires_at == NOW + timedelta(hours=100)
    # Simulate Graph expiring it.
    gw.set_clock(lambda: NOW + timedelta(hours=101))
    assert await gw.list_subscriptions() == []
    with pytest.raises(GraphNotFound):
        await gw.renew_subscription(sub.id, NOW + timedelta(hours=200))


async def test_delete_subscription(gw: MockGraphGateway) -> None:
    sub = await gw.create_subscription(
        resource="r", notification_url="u", client_state="c", expires_at=NOW + timedelta(hours=1)
    )
    await gw.delete_subscription(sub.id)
    assert await gw.list_subscriptions() == []
    with pytest.raises(GraphNotFound):
        await gw.delete_subscription(sub.id)


async def test_draft_and_send_are_separate_and_send_is_once(gw: MockGraphGateway) -> None:
    info = await gw.create_draft(
        OutboundDraft(to=["jane@acme.com"], subject="Deals", body_text="Hello Jane")
    )
    assert info.draft_id in gw.drafts and gw.drafts[info.draft_id]["sent"] is False
    assert gw.sent == [], "creating a draft never sends (INT-13)"
    result = await gw.send_draft(info.draft_id)
    assert result.sent and result.sent_at == NOW
    assert result.internet_message_id == info.internet_message_id
    assert len(gw.sent) == 1
    with pytest.raises(GraphNotFound):
        await gw.send_draft(info.draft_id)
    assert len(gw.sent) == 1, "a second send of the same draft is refused like Graph does"


async def test_send_unknown_draft_raises(gw: MockGraphGateway) -> None:
    with pytest.raises(GraphNotFound):
        await gw.send_draft("missing")


async def test_reply_draft_keeps_conversation_and_sets_in_reply_to(gw: MockGraphGateway) -> None:
    original = gw.inject_inbound(from_address="jane@acme.com", subject="Deals?", body="Any?")
    info = await gw.create_reply_draft(original.graph_id, "Yes, here are three.")
    assert info.conversation_id == original.conversation_id, "INV-23"
    draft = gw.drafts[info.draft_id]
    assert draft["to"] == ["jane@acme.com"]
    assert draft["subject"] == "RE: Deals?"
    assert draft["in_reply_to"] == original.external_message_id
    with pytest.raises(GraphNotFound):
        await gw.create_reply_draft("missing", "x")


async def test_create_draft_with_reply_to_or_conversation_threads_correctly(
    gw: MockGraphGateway,
) -> None:
    first = gw.inject_inbound(
        from_address="jane@acme.com", subject="Deals?", received_at=NOW - timedelta(hours=2)
    )
    latest = gw.inject_inbound(
        from_address="jane@acme.com",
        subject="RE: Deals?",
        conversation_id=first.conversation_id,
        received_at=NOW - timedelta(hours=1),
    )
    by_message = await gw.create_draft(
        OutboundDraft(
            to=["jane@acme.com"],
            subject="x",
            body_text="reply",
            reply_to_graph_message_id=first.graph_id,
        )
    )
    assert by_message.conversation_id == first.conversation_id
    by_conversation = await gw.create_draft(
        OutboundDraft(
            to=["jane@acme.com"],
            subject="x",
            body_text="reply",
            conversation_id=first.conversation_id,
        )
    )
    assert by_conversation.conversation_id == first.conversation_id
    assert gw.drafts[by_conversation.draft_id]["in_reply_to"] == latest.external_message_id
    fresh = await gw.create_draft(OutboundDraft(to=["new@x.com"], subject="Hi", body_text="b"))
    assert fresh.conversation_id not in {first.conversation_id, None}


async def test_fail_next_injects_one_failure(gw: MockGraphGateway) -> None:
    gw.fail_next("list_subscriptions", RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await gw.list_subscriptions()
    assert await gw.list_subscriptions() == []


async def test_calls_are_recorded_and_reset_clears(gw: MockGraphGateway) -> None:
    gw.inject_inbound(from_address="a@x.com")
    await gw.delta_messages()
    assert gw.calls[-1].method == "delta_messages"
    assert gw.calls[-1].args["folder"] == "inbox"
    gw.reset()
    assert gw.calls == [] and gw.messages == {} and gw.subscriptions == {}
