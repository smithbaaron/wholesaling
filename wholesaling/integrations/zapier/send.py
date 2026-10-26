"""Sending a parser email: always through the Graph draft -> send path (INT-13, RUL-06).

There is no Zapier client and no second mail transport. A parser email is an ordinary outbound
message from the deals mailbox to the Zapier Email Parser mailbox, so it is created as a Graph
draft and sent with the same activities every other outbound message uses: in mock mode the
mock gateway records it; in live mode ``send_draft`` refuses unless ``GRAPH_ALLOW_LIVE_SEND``
is true. Idempotent per ``(buy_box_id, version)`` through the draft idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass

from wholesaling.agents.a6 import ParserEmail


@dataclass(slots=True)
class ParserSendOutcome:
    message_id: str
    draft_id: str | None
    idempotency_key: str
    drafted: bool
    sent: bool
    replay: bool


async def send_parser_email_via_graph(
    email: ParserEmail, *, mailbox: str, thread_id: str | None = None
) -> ParserSendOutcome:
    """Draft + send one parser email. A second call with the same (buy box, version) finds the
    existing ``messages`` row and sends nothing (INT-22 replay acceptance)."""
    # Imported here: ``activities`` imports this package (activities/zapier.py), so a module-level
    # import would be circular.
    from wholesaling.activities.graph import (
        CreateDraftInput,
        SendDraftInput,
        create_draft,
        send_draft,
    )

    key = email.idempotency_key
    drafted = await create_draft(
        CreateDraftInput(
            idempotency_key=key,
            to=[mailbox],
            subject=email.subject,
            body_text=email.body,
            thread_id=thread_id,
        )
    )
    if drafted.already_sent:
        return ParserSendOutcome(
            message_id=drafted.message_id,
            draft_id=drafted.draft_id,
            idempotency_key=key,
            drafted=False,
            sent=False,
            replay=True,
        )
    sent = await send_draft(SendDraftInput(message_id=drafted.message_id))
    return ParserSendOutcome(
        message_id=drafted.message_id,
        draft_id=drafted.draft_id,
        idempotency_key=key,
        drafted=drafted.created,
        sent=sent.sent,
        replay=not drafted.created and sent.already_sent,
    )


__all__ = ["ParserSendOutcome", "send_parser_email_via_graph"]
