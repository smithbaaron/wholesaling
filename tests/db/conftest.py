"""Row factories for the DB tests. All run on the shared ``session`` fixture (SQLite by default)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.base import utcnow
from wholesaling.db.models import Investor, Message, Thread
from wholesaling.db.models.enums import Channel, Direction
from wholesaling.db.normalize import normalize_entity_name

MakeInvestor = Callable[..., Awaitable[Investor]]
MakeMessage = Callable[..., Awaitable[Message]]


@pytest.fixture
def make_investor(session: AsyncSession) -> MakeInvestor:
    async def _make(name: str = "Acme Holdings LLC", **kwargs: object) -> Investor:
        inv = Investor(
            canonical_name=name,
            entity_key=normalize_entity_name(name),
            aliases=[name],
            **kwargs,
        )
        session.add(inv)
        await session.flush()
        return inv

    return _make


@pytest.fixture
def make_message(session: AsyncSession) -> MakeMessage:
    async def _make(
        investor: Investor | None = None,
        body: str = "",
        external_message_id: str | None = None,
        thread: Thread | None = None,
    ) -> Message:
        if thread is None:
            thread = Thread(
                channel=Channel.EMAIL,
                external_thread_id=f"conv-{uuid.uuid4()}",
                investor_id=investor.id if investor else None,
            )
            session.add(thread)
            await session.flush()
        msg = Message(
            thread_id=thread.id,
            direction=Direction.IN,
            channel=Channel.EMAIL,
            external_message_id=external_message_id or f"msg-{uuid.uuid4()}",
            body=body,
            received_at=utcnow(),
        )
        session.add(msg)
        await session.flush()
        return msg

    return _make
