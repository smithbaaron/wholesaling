"""Review queue: list (HTML + JSON), filters, approve / edit-and-approve / reject, the
``human_decision`` signal contract, failure handling (BO-07, POL-01, INV-17/18)."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.conftest import OPERATOR, hours_ago, seed_market, seed_queued_message
from wholesaling.api.routes.review import (
    EVENT_APPROVED,
    EVENT_APPROVED_WITH_EDITS,
    EVENT_REJECTED,
    HUMAN_DECISION_SIGNAL,
)
from wholesaling.db.models.enums import Actor, PolicyDecision, QaVerdict
from wholesaling.db.models.messaging import Message
from wholesaling.db.models.ops import AuditEvent
from wholesaling.db.repositories.review import HumanDecision, ReviewRepository
from wholesaling.integrations.temporal.mock import MockTemporalGateway

SF = async_sessionmaker[AsyncSession]


async def _message(sf: SF, message_id: uuid.UUID) -> Message:
    async with sf() as s:
        row = await s.get(Message, message_id)
        assert row is not None
        return row


async def _audit(sf: SF, message_id: uuid.UUID) -> list[AuditEvent]:
    async with sf() as s:
        stmt = select(AuditEvent).where(
            AuditEvent.entity_type == "message", AuditEvent.entity_id == message_id
        )
        return list((await s.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------------------------


async def test_empty_queue_json_and_html(auth_client: AsyncClient) -> None:
    r = await auth_client.get("/api/review")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "items": [], "filters": {"action": None, "market": None}}
    page = await auth_client.get("/review")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "Queue is empty" in page.text


async def test_json_lists_seeded_items_with_qa_and_policy(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(
            s,
            qa_flags=["tone:salesy"],
            created_at=hours_ago(3),
            workflow_id="investor-sequence-abc",
        )
        await s.commit()
    r = await auth_client.get("/api/review")
    body = r.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["message_id"] == str(seeded.message.id)
    assert item["action"] == "investor_initial_outreach"
    assert item["recipient"] == "buyer@example.com"
    assert item["subject"] == "Off-market deals in Houston"
    assert item["body_preview"].startswith("Hi, we source")
    assert item["qa"] == {
        "verdict": "approve",
        "confidence": 0.91,
        "flags": ["tone:salesy"],
        "rationale": "Accurate, compliant, matches the thread.",
    }
    assert item["policy_reason"] == "draft at L0: human sends"
    assert item["workflow_id"] == "investor-sequence-abc"
    assert item["sequence_step"] == 0
    assert item["market_name"] == "Houston, TX"
    assert item["investor_name"] == "Lone Star Holdings LLC"
    assert 2.9 * 3600 < item["age_seconds"] < 3.2 * 3600


async def test_non_queued_rows_are_excluded(auth_client: AsyncClient, session_factory: SF) -> None:
    async with session_factory() as s:
        await seed_queued_message(s, policy_decision=PolicyDecision.SEND)  # auto-sent
        await seed_queued_message(s, draft=False)  # already sent
        await seed_queued_message(s, policy_decision=PolicyDecision.BLOCKED)
        keep = await seed_queued_message(s)
        await s.commit()
    r = await auth_client.get("/api/review")
    assert [i["message_id"] for i in r.json()["items"]] == [str(keep.message.id)]


async def test_filter_by_action_and_market(auth_client: AsyncClient, session_factory: SF) -> None:
    async with session_factory() as s:
        houston = await seed_market(s, "Houston", "TX")
        dallas = await seed_market(s, "Dallas", "TX")
        a = await seed_queued_message(s, market=houston, action="investor_initial_outreach")
        b = await seed_queued_message(s, market=dallas, action="investor_followup")
        c = await seed_queued_message(s, market=houston, action="investor_followup")
        await s.commit()
    r = await auth_client.get("/api/review", params={"action": "investor_followup"})
    assert {i["message_id"] for i in r.json()["items"]} == {str(b.message.id), str(c.message.id)}
    r = await auth_client.get("/api/review", params={"market": str(houston.id)})
    assert {i["message_id"] for i in r.json()["items"]} == {str(a.message.id), str(c.message.id)}
    r = await auth_client.get(
        "/api/review", params={"market": str(dallas.id), "action": "investor_followup"}
    )
    assert [i["message_id"] for i in r.json()["items"]] == [str(b.message.id)]
    r = await auth_client.get("/api/review", params={"market": "not-a-uuid"})
    assert r.status_code == 422


async def test_html_renders_key_fields_grouped_by_action(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    async with session_factory() as s:
        first = await seed_queued_message(
            s,
            action="investor_reply",
            recipient="reply@example.com",
            subject="Re: your buy box",
            qa_verdict=QaVerdict.REWRITE,
            qa_confidence=Decimal("0.42"),
            qa_flags=["compliance:unsubstantiated_claim"],
            qa_rationale="Claims a deal that is not in the facts.",
            policy_reason="QA asked for a rewrite",
        )
        await seed_queued_message(s, action="investor_initial_outreach")
        await s.commit()
    r = await auth_client.get("/review")
    html = r.text
    assert 'id="group-investor_initial_outreach"' in html
    assert 'id="group-investor_reply"' in html
    assert html.index("group-investor_initial_outreach") < html.index("group-investor_reply")
    assert "reply@example.com" in html
    assert "Re: your buy box" in html
    assert "QA rewrite" in html
    assert "confidence 42%" in html
    assert "compliance:unsubstantiated_claim" in html
    assert "Claims a deal that is not in the facts." in html
    assert "Policy: QA asked for a rewrite" in html
    assert "Houston, TX" in html
    assert f'hx-post="/review/{first.message.id}/approve"' in html
    assert f'hx-post="/review/{first.message.id}/reject"' in html
    assert 'name="edited_body"' in html
    assert "/static/htmx.min.js" in html
    assert "2 drafts waiting" in html
    assert OPERATOR in html


async def test_html_market_filter_dropdown_lists_only_markets_in_queue(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    async with session_factory() as s:
        houston = await seed_market(s, "Houston", "TX")
        await seed_market(s, "Austin", "TX")
        await seed_queued_message(s, market=houston)
        await s.commit()
    html = (await auth_client.get("/review")).text
    assert "Houston, TX" in html
    assert "Austin, TX" not in html


# --------------------------------------------------------------------------------------------
# Approve
# --------------------------------------------------------------------------------------------


async def test_approve_signals_sequence_workflow_and_flips_row(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="investor-sequence-xyz", step=2)
        await s.commit()
    mid = seeded.message.id

    r = await auth_client.post(f"/review/{mid}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "ok": True,
        "message_id": str(mid),
        "workflow_id": "investor-sequence-xyz",
        "approved": True,
        "edited": False,
        "policy_decision": "send",
        "event_type": EVENT_APPROVED,
    }

    # Exactly one signal, to the sequence's workflow id, named human_decision, with the dataclass.
    signals = temporal.signals["investor-sequence-xyz"]
    assert len(signals) == 1
    name, args = signals[0]
    assert name == HUMAN_DECISION_SIGNAL == "human_decision"
    (payload,) = args
    assert isinstance(payload, HumanDecision)
    assert asdict(payload) == {
        "approved": True,
        "edited_body": None,
        "step": 2,
        "message_id": str(mid),
        "actor_id": OPERATOR,
    }

    row = await _message(session_factory, mid)
    assert row.policy_decision is PolicyDecision.SEND
    assert row.body == seeded.message.body  # untouched
    assert row.meta["human_decision"]["approved"] is True
    assert row.meta["human_decision"]["operator"] == OPERATOR

    events = await _audit(session_factory, mid)
    assert len(events) == 1
    ev = events[0]
    assert ev.actor is Actor.HUMAN
    assert ev.actor_id == OPERATOR
    assert ev.event_type == EVENT_APPROVED
    assert ev.payload["workflow_id"] == "investor-sequence-xyz"
    assert ev.payload["action"] == "investor_initial_outreach"
    assert ev.payload["edited"] is False

    # The item has left the queue.
    assert (await auth_client.get("/api/review")).json()["count"] == 0


async def test_edit_and_approve_carries_edited_body(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-edit")
        await s.commit()
    mid = seeded.message.id
    new_body = "Hi there,\n\nShorter and friendlier.\n\nAaron"
    r = await auth_client.post(f"/review/{mid}/approve", data={"edited_body": new_body})
    assert r.status_code == 200
    assert r.json()["edited"] is True
    assert r.json()["event_type"] == EVENT_APPROVED_WITH_EDITS
    (_, args) = temporal.signals["wf-edit"][0]
    payload = args[0]
    assert payload.approved is True and payload.edited_body == new_body
    row = await _message(session_factory, mid)
    assert row.body == new_body
    assert row.meta["human_decision"]["edited"] is True
    events = await _audit(session_factory, mid)
    assert [e.event_type for e in events] == [EVENT_APPROVED_WITH_EDITS]


async def test_unchanged_textarea_is_not_an_edit(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-same", body="Same body.")
        await s.commit()
    r = await auth_client.post(
        f"/review/{seeded.message.id}/approve", data={"edited_body": "  Same body.  "}
    )
    assert r.status_code == 200
    assert r.json()["edited"] is False
    assert temporal.signals["wf-same"][0][1][0].edited_body is None


async def test_approve_htmx_request_returns_fragment(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-htmx")
        await s.commit()
    r = await auth_client.post(
        f"/review/{seeded.message.id}/approve", headers={"HX-Request": "true"}
    )
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert f'id="item-{seeded.message.id}"' in r.text  # swaps the row out
    assert "Approved" in r.text and "wf-htmx" in r.text
    assert "<html" not in r.text  # a fragment, not a page


# --------------------------------------------------------------------------------------------
# Reject
# --------------------------------------------------------------------------------------------


async def test_reject_blocks_row_and_signals_not_approved(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-reject", step=1)
        await s.commit()
    mid = seeded.message.id
    r = await auth_client.post(f"/review/{mid}/reject", data={"reason": "wrong recipient"})
    assert r.status_code == 200
    assert r.json()["approved"] is False
    assert r.json()["policy_decision"] == "blocked"
    (name, args) = temporal.signals["wf-reject"][0]
    assert name == "human_decision"
    assert asdict(args[0]) == {
        "approved": False,
        "edited_body": None,
        "step": 1,
        "message_id": str(mid),
        "actor_id": OPERATOR,
    }
    row = await _message(session_factory, mid)
    assert row.policy_decision is PolicyDecision.BLOCKED
    assert row.meta["human_decision"]["reason"] == "wrong recipient"
    events = await _audit(session_factory, mid)
    assert [e.event_type for e in events] == [EVENT_REJECTED]
    assert events[0].payload["reason"] == "wrong recipient"
    assert events[0].actor is Actor.HUMAN and events[0].actor_id == OPERATOR


async def test_reject_htmx_fragment_mentions_reason(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-r2")
        await s.commit()
    r = await auth_client.post(
        f"/review/{seeded.message.id}/reject",
        data={"reason": "stale facts"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "Rejected" in r.text and "stale facts" in r.text


# --------------------------------------------------------------------------------------------
# Workflow id resolution for inbound-reply drafts (no sequence)
# --------------------------------------------------------------------------------------------


async def test_inbound_reply_draft_uses_meta_workflow_id(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(
            s,
            action="investor_reply",
            with_sequence=False,
            meta={"temporal_workflow_id": "graph-inbound-sub-1-msg-9"},
        )
        await s.commit()
    r = await auth_client.post(f"/review/{seeded.message.id}/approve")
    assert r.status_code == 200
    assert r.json()["workflow_id"] == "graph-inbound-sub-1-msg-9"
    payload = temporal.signals["graph-inbound-sub-1-msg-9"][0][1][0]
    assert payload.step is None and payload.message_id == str(seeded.message.id)


async def test_inbound_reply_draft_falls_back_to_audit_event(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(
            s,
            action="investor_reply",
            with_sequence=False,
            inbound_workflow_audit="graph-inbound-sub-1-msg-10",
        )
        await s.commit()
    r = await auth_client.post(f"/review/{seeded.message.id}/approve")
    assert r.status_code == 200
    assert r.json()["workflow_id"] == "graph-inbound-sub-1-msg-10"
    assert "graph-inbound-sub-1-msg-10" in temporal.signals


async def test_sequence_wins_over_meta_and_audit(session_factory: SF) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(
            s,
            workflow_id="seq-wf",
            meta={"temporal_workflow_id": "meta-wf"},
            inbound_workflow_audit="audit-wf",
        )
        assert await ReviewRepository(s).resolve_workflow_id(seeded.message) == "seq-wf"


async def test_no_resolvable_workflow_is_422_and_row_unchanged(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, action="investor_reply", with_sequence=False)
        await s.commit()
    r = await auth_client.post(f"/review/{seeded.message.id}/approve")
    assert r.status_code == 422
    assert "no owning workflow" in r.json()["detail"]
    assert temporal.signals == {}
    row = await _message(session_factory, seeded.message.id)
    assert row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN


# --------------------------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------------------------


async def test_signal_failure_leaves_row_unchanged_and_returns_502(
    auth_client: AsyncClient,
    session_factory: SF,
    temporal: MockTemporalGateway,
    monkeypatch: object,
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-down")
        await s.commit()

    async def boom(workflow_id: str, signal: str, *args: object) -> None:
        raise RuntimeError("temporal unreachable")

    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(temporal, "signal", boom)

    r = await auth_client.post(f"/review/{seeded.message.id}/approve", data={"edited_body": "x"})
    assert r.status_code == 502
    assert "wf-down" in r.json()["detail"]
    row = await _message(session_factory, seeded.message.id)
    assert row.policy_decision is PolicyDecision.QUEUE_FOR_HUMAN
    assert row.body == seeded.message.body
    assert "human_decision" not in row.meta
    assert await _audit(session_factory, seeded.message.id) == []
    # Still in the queue for a retry.
    assert (await auth_client.get("/api/review")).json()["count"] == 1

    # HTMX variant: 502 with an error fragment that keeps the row id so it swaps in place.
    r = await auth_client.post(
        f"/review/{seeded.message.id}/reject",
        data={"reason": "r"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 502
    assert "Not applied" in r.text and f'id="item-{seeded.message.id}"' in r.text


async def test_second_decision_on_same_message_is_409(
    auth_client: AsyncClient, session_factory: SF, temporal: MockTemporalGateway
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-once")
        await s.commit()
    assert (await auth_client.post(f"/review/{seeded.message.id}/approve")).status_code == 200
    r = await auth_client.post(f"/review/{seeded.message.id}/reject", data={"reason": "late"})
    assert r.status_code == 409
    assert len(temporal.signals["wf-once"]) == 1


async def test_unknown_message_is_404(auth_client: AsyncClient) -> None:
    r = await auth_client.post(f"/review/{uuid.uuid4()}/approve")
    assert r.status_code == 404


async def test_malformed_message_id_is_422(auth_client: AsyncClient) -> None:
    r = await auth_client.post("/review/not-a-uuid/approve")
    assert r.status_code == 422


# --------------------------------------------------------------------------------------------
# Repository helpers
# --------------------------------------------------------------------------------------------


async def test_queue_depth_and_oldest_age(session_factory: SF) -> None:
    async with session_factory() as s:
        repo = ReviewRepository(s)
        assert await repo.queue_depth() == 0
        assert await repo.oldest_queued_at() is None
        await seed_queued_message(s, created_at=hours_ago(5))
        await seed_queued_message(s, created_at=hours_ago(1))
        await seed_queued_message(s, created_at=hours_ago(9), policy_decision=PolicyDecision.SEND)
        assert await repo.queue_depth() == 2
        oldest = await repo.oldest_queued_at()
        assert oldest is not None
        assert abs((hours_ago(5) - oldest).total_seconds()) < 5


async def test_recipient_falls_back_to_thread_counterpart(session_factory: SF) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s)
        seeded.message.to_addresses = []
        await s.flush()
        item = await ReviewRepository(s).get_item(seeded.message.id)
        assert item is not None
        assert item.recipient == "buyer@example.com"
        assert item.as_dict()["recipient"] == "buyer@example.com"


async def test_plain_browser_form_post_redirects_to_queue(
    auth_client: AsyncClient, session_factory: SF
) -> None:
    async with session_factory() as s:
        seeded = await seed_queued_message(s, workflow_id="wf-noscript")
        await s.commit()
    r = await auth_client.post(
        f"/review/{seeded.message.id}/approve", headers={"Accept": "text/html"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/review"
    row = await _message(session_factory, seeded.message.id)
    assert row.policy_decision is PolicyDecision.SEND
