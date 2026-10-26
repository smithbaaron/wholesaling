"""``send_parser_email``: RUL-07 flag off -> no send + audit note; flag on (mock Graph) -> draft
and send recorded through the Graph path; replay is a no-op (INT-22)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integrations.sheets.conftest import BB_V2_ID, INVESTOR_ID, NOW, Seed, seed_projection
from wholesaling.activities.graph import GraphActivityContext, set_graph_context
from wholesaling.activities.zapier import (
    PARSER_SENT_EVENT,
    PARSER_SKIPPED_EVENT,
    REASON_FLAG_OFF,
    REASON_NO_MAILBOX,
    REASON_NOT_FOUND,
    REASON_REPLAY,
    SendParserEmailInput,
    SendParserEmailResult,
    ZapierActivityContext,
    send_parser_email,
    set_zapier_context,
)
from wholesaling.agents.a6 import parser_idempotency_key
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, BuyBox, Message
from wholesaling.db.models.enums import Direction
from wholesaling.integrations.graph import MockGraphGateway

SF = async_sessionmaker[AsyncSession]
MAILBOX = "deals@example.test"
PARSER_MAILBOX = "abc123@robot.zapier.com"


def _settings(**extra: object) -> Settings:
    return Settings(_env_file=None, app_env="test", graph_mailbox=MAILBOX, **extra)  # type: ignore[call-arg]


@pytest.fixture
def graph() -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=lambda: NOW)


def _install(session_factory: SF, graph: MockGraphGateway, settings: Settings) -> None:
    set_graph_context(
        GraphActivityContext(
            gateway=graph, session_factory=session_factory, settings=settings, clock=lambda: NOW
        )
    )
    set_zapier_context(
        ZapierActivityContext(session_factory=session_factory, settings=settings, clock=lambda: NOW)
    )


@pytest.fixture
def flag_off(session_factory: SF, graph: MockGraphGateway) -> Iterator[Settings]:
    s = _settings(zapier_parser_address=PARSER_MAILBOX)
    _install(session_factory, graph, s)
    yield s
    set_graph_context(None)
    set_zapier_context(None)


@pytest.fixture
def flag_on(session_factory: SF, graph: MockGraphGateway) -> Iterator[Settings]:
    s = _settings(zapier_parser_enabled=True, zapier_parser_address=PARSER_MAILBOX)
    _install(session_factory, graph, s)
    yield s
    set_graph_context(None)
    set_zapier_context(None)


@pytest.fixture
async def seed(session: AsyncSession) -> Seed:
    return await seed_projection(session)


async def _events(sf: SF, event_type: str) -> list[AuditEvent]:
    async with sf() as s:
        stmt = select(AuditEvent).where(AuditEvent.event_type == event_type)
        return list((await s.execute(stmt)).scalars().all())


async def _outbound(sf: SF) -> list[Message]:
    async with sf() as s:
        stmt = select(Message).where(Message.direction == Direction.OUT, Message.draft.is_(False))
        return [
            m for m in (await s.execute(stmt)).scalars().all() if m.to_addresses == [PARSER_MAILBOX]
        ]


def test_default_settings_have_the_flag_off() -> None:
    assert _settings().zapier_parser_enabled is False  # RUL-07


async def test_flag_off_sends_nothing_and_records_an_audit_note(
    flag_off: Settings, seed: Seed, graph: MockGraphGateway, session_factory: SF
) -> None:
    result = await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    assert isinstance(result, SendParserEmailResult)
    assert result.enabled is False and result.sent is False
    assert result.skipped_reason == REASON_FLAG_OFF and result.version == 2
    assert result.audit_created is True
    assert graph.calls == [] and graph.drafts == {} and graph.sent == []
    assert await _outbound(session_factory) == []
    [note] = await _events(session_factory, PARSER_SKIPPED_EVENT)
    assert note.entity_id == BB_V2_ID and note.actor_id == "A6"
    assert note.payload["reason"] == REASON_FLAG_OFF
    assert "RUL-07" in note.payload["note"]
    assert note.idempotency_key == f"{parser_idempotency_key(str(BB_V2_ID), 2)}:skipped"
    # Replaying the skipped event does not add a second note.
    again = await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    assert again.audit_created is False
    assert len(await _events(session_factory, PARSER_SKIPPED_EVENT)) == 1


async def test_flag_on_without_mailbox_is_skipped(
    session_factory: SF, graph: MockGraphGateway, seed: Seed
) -> None:
    _install(session_factory, graph, _settings(zapier_parser_enabled=True))
    try:
        result = await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    finally:
        set_graph_context(None)
        set_zapier_context(None)
    assert result.sent is False and result.skipped_reason == REASON_NO_MAILBOX
    assert graph.sent == []


async def test_flag_on_in_mock_mode_drafts_then_sends_through_graph(
    flag_on: Settings, seed: Seed, graph: MockGraphGateway, session_factory: SF
) -> None:
    result = await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    assert result.enabled and result.sent and result.skipped_reason is None
    assert result.subject == f"[BUYBOX] {INVESTOR_ID} default v2"
    assert result.idempotency_key == f"zapier-parser:{BB_V2_ID}:v2"
    # Draft and send are separate Graph calls (INT-13), both recorded by the mock.
    assert [c.method for c in graph.calls] == ["create_draft", "send_draft"]
    draft_args = graph.calls[0].args["draft"]
    assert draft_args["to"] == [PARSER_MAILBOX]
    assert draft_args["body_text"].startswith("BEGIN_BUY_BOX\nFORMAT_VERSION: 1\n")
    assert f"BUY_BOX_ID: {BB_V2_ID}" in draft_args["body_text"]
    assert len(graph.sent) == 1 and graph.sent[0].sent
    # One messages row, sent, keyed by the idempotency key; one zapier.parser_sent event.
    [row] = await _outbound(session_factory)
    assert row.external_message_id == result.idempotency_key and row.sent_at is not None
    assert row.from_address == MAILBOX and str(row.id) == result.message_id
    [event] = await _events(session_factory, PARSER_SENT_EVENT)
    assert event.payload["to"] == PARSER_MAILBOX and event.payload["version"] == 2
    assert event.payload["graph_mode"] == "mock"
    assert await _events(session_factory, PARSER_SKIPPED_EVENT) == []


async def test_zapier_replay_is_noop(
    flag_on: Settings, seed: Seed, graph: MockGraphGateway, session_factory: SF
) -> None:
    first = await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    replay = await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    assert first.sent is True
    assert replay.sent is False and replay.replayed and replay.skipped_reason == REASON_REPLAY
    assert replay.message_id == first.message_id
    assert replay.audit_created is False
    assert len(graph.sent) == 1, "the mailbox saw exactly one parser email"
    assert [c.method for c in graph.calls] == ["create_draft", "send_draft"]
    assert len(await _outbound(session_factory)) == 1
    assert len(await _events(session_factory, PARSER_SENT_EVENT)) == 1


async def test_new_version_sends_a_new_email_to_the_same_row_key(
    flag_on: Settings, seed: Seed, graph: MockGraphGateway, session_factory: SF
) -> None:
    await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    async with session_factory() as s:
        current = await s.get(BuyBox, BB_V2_ID)
        assert current is not None
        current.is_current = False
        v3 = BuyBox(
            investor_id=INVESTOR_ID,
            buy_box_key="default",
            version=3,
            geography=current.geography,
            asset_types=current.asset_types,
            price_max=current.price_max,
        )
        s.add(v3)
        await s.commit()
        v3_id = v3.id
    result = await send_parser_email(SendParserEmailInput(buy_box_id=str(v3_id)))
    assert result.sent and result.version == 3
    assert len(graph.sent) == 2
    bodies = [d["body"] for d in graph.drafts.values()]
    assert any("VERSION: 2\n" in b for b in bodies) and any("VERSION: 3\n" in b for b in bodies)
    # Same logical buy box: both emails carry the same ROW_KEY so the Zap updates one row.
    assert all(f"ROW_KEY: {INVESTOR_ID}|default\n" in b for b in bodies)


async def test_unknown_buy_box_is_reported_not_raised(
    flag_on: Settings, graph: MockGraphGateway
) -> None:
    result = await send_parser_email(SendParserEmailInput(buy_box_id=str(uuid.uuid4())))
    assert result.sent is False and result.skipped_reason == REASON_NOT_FOUND
    assert result.version is None and graph.calls == []


async def test_audit_row_count_is_bounded_on_replay(
    flag_on: Settings, seed: Seed, session_factory: SF
) -> None:
    async with session_factory() as s:
        before = int((await s.execute(select(func.count(AuditEvent.id)))).scalar_one())
    await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    async with session_factory() as s:
        after_first = int((await s.execute(select(func.count(AuditEvent.id)))).scalar_one())
    await send_parser_email(SendParserEmailInput(buy_box_id=str(BB_V2_ID)))
    async with session_factory() as s:
        after_replay = int((await s.execute(select(func.count(AuditEvent.id)))).scalar_one())
    # drafted + sent (graph) + parser_sent (zapier) on the first call; nothing on replay.
    assert after_first - before == 3
    assert after_replay == after_first
