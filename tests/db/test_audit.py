"""Audit log: append-only, idempotent on replay (INV-24)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models import AuditEvent
from wholesaling.db.models.enums import Actor
from wholesaling.db.repositories.audit import AuditRepository


async def _count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(AuditEvent))).scalar_one())


async def test_record_and_replay(session: AsyncSession) -> None:
    repo = AuditRepository(session)
    entity = uuid.uuid4()
    ev, created = await repo.record(
        actor=Actor.AGENT,
        actor_id="A4",
        event_type="message.classified",
        entity_type="message",
        entity_id=entity,
        payload={"classification": "interested"},
        idempotency_key="graph:AAMk-123:classified",
    )
    assert created and ev.created_at is not None
    again, created2 = await repo.record(
        actor=Actor.AGENT,
        event_type="message.classified",
        entity_type="message",
        entity_id=entity,
        payload={"classification": "DIFFERENT"},
        idempotency_key="graph:AAMk-123:classified",
    )
    assert created2 is False
    assert again.id == ev.id
    assert again.payload == {"classification": "interested"}, "replay never overwrites"
    assert await _count(session) == 1
    assert [e.id for e in await repo.for_entity("message", entity)] == [ev.id]


async def test_events_without_key_always_append(session: AsyncSession) -> None:
    repo = AuditRepository(session)
    for _ in range(3):
        await repo.record(actor=Actor.SYSTEM, event_type="tick", entity_type="scheduler")
    assert await _count(session) == 3


async def test_idempotency_key_unique_at_db_level(session: AsyncSession) -> None:
    session.add(AuditEvent(actor=Actor.HUMAN, event_type="x", entity_type="y", idempotency_key="k"))
    await session.flush()
    session.add(AuditEvent(actor=Actor.HUMAN, event_type="x", entity_type="y", idempotency_key="k"))
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
