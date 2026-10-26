"""Signal payloads shared by the inbound pipeline and ``InvestorSequenceWorkflow`` (WF-19,
WF-21; docs/agents/conversation.md "Signal contracts").

One module, imported by both sides, so the sender (``activities/conversation.py``) and the
receiver (``workflows/investor_sequence.py``) cannot drift apart again (pilot GAP-01). Temporal's
default converter serialises each payload as a JSON object keyed by field name and maps it back
onto the handler's annotated type; the handlers also accept *no* payload (``None``) so a plain
``client.signal(id, "reply")`` from an operator tool keeps working.

Pure dataclasses, no imports beyond the standard library, and deliberately outside both the
``activities`` and ``workflows`` packages (each imports the other's package at import time, so a
module inside either would form a cycle).
"""

from __future__ import annotations

from dataclasses import dataclass

SIGNAL_REPLY = "reply"
SIGNAL_OPT_OUT = "opt_out"
SIGNAL_BOUNCE = "bounce"


@dataclass(slots=True)
class ReplySignal:
    """A substantive inbound reply arrived on the sequence's thread (never an auto-reply)."""

    message_id: str
    classification: str


@dataclass(slots=True)
class OptOutSignal:
    """The recipient opted out; the suppression row is already written."""

    message_id: str


@dataclass(slots=True)
class BounceSignal:
    """An NDR for the sequence's recipient."""

    message_id: str
    bounced_address: str | None = None


SequenceSignal = ReplySignal | OptOutSignal | BounceSignal

__all__ = [
    "SIGNAL_BOUNCE",
    "SIGNAL_OPT_OUT",
    "SIGNAL_REPLY",
    "BounceSignal",
    "OptOutSignal",
    "ReplySignal",
    "SequenceSignal",
]
