"""Graph model translation and bounce detection (INT-12, INT-25, XC-03)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wholesaling.integrations.graph import (
    MAX_SUBSCRIPTION_LIFETIME,
    ChangeNotification,
    InboundMessage,
    NotificationBatch,
    Subscription,
    detect_bounce,
    subscription_expiry,
)

GRAPH_MESSAGE = {
    "id": "AAMkAGI2THVSAAA=",
    "internetMessageId": "<abc123@mail.example.com>",
    "conversationId": "AAQkAGI2THVSAAA=",
    "subject": "RE: Off-market deals in Houston",
    "receivedDateTime": "2026-10-25T14:03:11Z",
    "isDraft": False,
    "isRead": False,
    "from": {"emailAddress": {"name": "Jane Investor", "address": "jane@acmeholdings.com"}},
    "toRecipients": [{"emailAddress": {"address": "deals@northstack-deals.com"}}],
    "ccRecipients": [{"emailAddress": {"address": "ops@acmeholdings.com"}}],
    "bodyPreview": "Yes, send me what you have.",
    "body": {"contentType": "html", "content": "<p>Yes, <b>send</b> me what you have.</p>"},
    "internetMessageHeaders": [
        {"name": "Message-ID", "value": "<abc123@mail.example.com>"},
        {"name": "In-Reply-To", "value": "<orig@northstack-deals.com>"},
        {"name": "References", "value": "<orig@northstack-deals.com> <x@y.z>"},
        {"name": "X-Something-Irrelevant", "value": "dropped"},
        {"name": "Content-Type", "value": 'multipart/alternative; boundary="b"'},
    ],
}


def test_from_graph_maps_ids_addresses_and_headers() -> None:
    msg = InboundMessage.from_graph(GRAPH_MESSAGE, own_mailbox="deals@northstack-deals.com")
    assert msg.graph_id == "AAMkAGI2THVSAAA="
    assert msg.external_message_id == "<abc123@mail.example.com>"
    assert msg.conversation_id == "AAQkAGI2THVSAAA="
    assert msg.from_address == "jane@acmeholdings.com"
    assert msg.from_name == "Jane Investor"
    assert msg.to == ["deals@northstack-deals.com"]
    assert msg.cc == ["ops@acmeholdings.com"]
    assert msg.received_at == datetime(2026, 10, 25, 14, 3, 11, tzinfo=UTC)
    assert msg.in_reply_to == "<orig@northstack-deals.com>"
    assert msg.references == ["<orig@northstack-deals.com>", "<x@y.z>"]
    assert msg.is_reply
    assert "x-something-irrelevant" not in msg.headers, "only the header subset is kept"
    assert msg.body_html is not None and "<b>" in msg.body_html
    assert msg.body_text == "Yes, send me what you have."
    assert not msg.is_bounce
    assert msg.raw["id"] == "AAMkAGI2THVSAAA="


def test_from_graph_falls_back_to_graph_id_when_no_internet_message_id() -> None:
    payload = {"id": "DRAFT1", "isDraft": True, "body": {"contentType": "text", "content": "x"}}
    msg = InboundMessage.from_graph(payload)
    assert msg.external_message_id == "graph:DRAFT1"
    assert msg.is_draft
    assert msg.body_text == "x"
    assert msg.received_at is None


def test_text_body_without_preview_is_kept_verbatim() -> None:
    payload = {
        "id": "T1",
        "internetMessageId": "<t1@x.y>",
        "body": {"contentType": "text", "content": "line one\nline two"},
    }
    assert InboundMessage.from_graph(payload).body_text == "line one\nline two"


@pytest.mark.parametrize(
    ("from_address", "subject", "headers", "expected_reason"),
    [
        ("postmaster@outlook.com", "Undeliverable: Off-market deals", {}, "header_or_sender"),
        ("MAILER-DAEMON@mx.google.com", "Delivery Status Notification (Failure)", {}, "sender"),
        ("someone@else.com", "Undeliverable: hello", {}, "subject"),
        (
            "someone@else.com",
            "anything",
            {"Content-Type": 'multipart/report; report-type=delivery-status; boundary="x"'},
            "header",
        ),
    ],
)
def test_detect_bounce_signals(
    from_address: str, subject: str, headers: dict[str, str], expected_reason: str
) -> None:
    info = detect_bounce(
        from_address=from_address,
        subject=subject,
        headers=headers,
        body_text="Delivery to the following recipient failed permanently: bob@dead-domain.com",
    )
    assert info.is_bounce
    assert info.bounced_address == "bob@dead-domain.com"
    assert info.reason in {"header", "sender", "subject"}
    if expected_reason != "header_or_sender":
        assert info.reason == expected_reason


def test_detect_bounce_prefers_x_failed_recipients_header() -> None:
    info = detect_bounce(
        from_address="postmaster@example.com",
        subject="Undeliverable",
        headers={"X-Failed-Recipients": "first@dead.com, second@dead.com"},
        body_text="no address in the body",
    )
    assert info.bounced_address == "first@dead.com"


def test_detect_bounce_body_fallback_skips_own_and_sender_addresses() -> None:
    info = detect_bounce(
        from_address="postmaster@northstack-deals.com",
        subject="Undeliverable: hi",
        headers={},
        body_text=(
            "Your message to deals@northstack-deals.com was returned. "
            "Contact postmaster@northstack-deals.com. Target: gone@nowhere.org."
        ),
        own_mailbox="deals@northstack-deals.com",
    )
    assert info.bounced_address == "gone@nowhere.org"


def test_plain_reply_mentioning_undeliverable_is_not_a_bounce() -> None:
    info = detect_bounce(
        from_address="jane@acmeholdings.com",
        subject="RE: Off-market deals",
        headers={"Content-Type": "text/plain"},
        body_text="Last time your mail was undeliverable, glad it works now.",
    )
    assert not info.is_bounce
    assert info.bounced_address is None


def test_change_notification_parses_graph_shape_and_resource_id() -> None:
    batch = NotificationBatch.model_validate(
        {
            "value": [
                {
                    "subscriptionId": "sub-1",
                    "clientState": "secret",
                    "changeType": "created",
                    "resource": "Users/mbx/Messages/AAMk1",
                    "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": "AAMk1"},
                    "subscriptionExpirationDateTime": "2026-10-28T00:00:00.000Z",
                    "tenantId": "t",
                }
            ]
        }
    )
    note = batch.value[0]
    assert note.subscription_id == "sub-1"
    assert note.client_state == "secret"
    assert note.resource_id == "AAMk1"
    assert note.dedupe_key == ("sub-1", "AAMk1")
    assert not note.is_lifecycle
    assert note.subscription_expiration == datetime(2026, 10, 28, tzinfo=UTC)


def test_change_notification_resource_id_from_resource_path_forms() -> None:
    a = ChangeNotification(subscriptionId="s", resource="Users/u/Messages/ABC=")  # type: ignore[call-arg]
    b = ChangeNotification(subscriptionId="s", resource="Users('u')/Messages('XYZ')")  # type: ignore[call-arg]
    c = ChangeNotification(subscriptionId="s", lifecycleEvent="missed")  # type: ignore[call-arg]
    assert a.resource_id == "ABC="
    assert b.resource_id == "XYZ"
    assert c.resource_id is None and c.is_lifecycle


def test_subscription_from_graph_and_expiry_window() -> None:
    sub = Subscription.from_graph(
        {
            "id": "sub-9",
            "resource": "users/x/mailFolders('inbox')/messages",
            "changeType": "created,updated",
            "expirationDateTime": "2026-10-27T12:00:00.0000000Z",
            "notificationUrl": "https://api.example/webhooks/graph",
        }
    )
    assert sub.change_types == ["created", "updated"]
    assert sub.expires_at == datetime(2026, 10, 27, 12, tzinfo=UTC)
    now = datetime(2026, 10, 27, 11, tzinfo=UTC)
    assert sub.expires_within(now, 3600)
    assert not sub.expires_within(now, 1800)


def test_subscription_expiry_is_capped_at_graph_maximum() -> None:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    assert subscription_expiry(now) == now + timedelta(hours=70)
    assert subscription_expiry(now, timedelta(days=30)) == now + MAX_SUBSCRIPTION_LIFETIME
    assert timedelta(minutes=10_080) == MAX_SUBSCRIPTION_LIFETIME
