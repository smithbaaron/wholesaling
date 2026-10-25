"""Append-only audit log (REQUIREMENTS.md INV-16 "audit", INV-24 "exactly one logged event")."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import Actor
from wholesaling.db.models.ops import AuditEvent


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        *,
        actor: Actor,
        event_type: str,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        actor_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[AuditEvent, bool]:
        """Append one event. Returns ``(event, created)``.

        With an ``idempotency_key`` (a Temporal activity id, a Graph message id, a form
        submission id) a replay returns the original event and writes nothing, so retried
        activities never double-log.
        """
        if idempotency_key is not None:
            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing, False
        event = AuditEvent(
            actor=actor,
            actor_id=actor_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=dict(payload or {}),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        self._s.add(event)
        await self._s.flush()
        return event, True

    async def get_by_idempotency_key(self, idempotency_key: str) -> AuditEvent | None:
        stmt = select(AuditEvent).where(AuditEvent.idempotency_key == idempotency_key)
        return (await self._s.execute(stmt)).scalar_one_or_none()

    async def for_entity(self, entity_type: str, entity_id: uuid.UUID) -> list[AuditEvent]:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.entity_type == entity_type, AuditEvent.entity_id == entity_id)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        return list((await self._s.execute(stmt)).scalars().all())
