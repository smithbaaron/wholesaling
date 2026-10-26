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

# --------------------------------------------------------------------------------------------
# DispositionWorkflow (WF-06) signals - sent by the /deals operator routes and, later, by the
# inbound pipeline when a buyer replies. Every payload is optional on the wire so a bare
# ``client.signal(id, "deal_dead")`` from an operator tool keeps working.
# --------------------------------------------------------------------------------------------

SIGNAL_BUYER_RESPONSE = "buyer_response"
SIGNAL_BUYER_SELECTED = "buyer_selected"
SIGNAL_DEPOSIT_RECEIVED = "deposit_received"
SIGNAL_DEAL_DEAD = "deal_dead"
SIGNAL_DEAL_CLOSED = "deal_closed"
SIGNAL_DEADLINES_UPDATED = "deadlines_updated"
SIGNAL_HUMAN_DECISION = "human_decision"


@dataclass(slots=True)
class BuyerResponseSignal:
    """A buyer answered the blast; the row is already written by ``record_buyer_response``."""

    investor_id: str
    response: str  # "interested" | "pass"
    proof_of_funds: bool = False
    deposit_terms: str | None = None


@dataclass(slots=True)
class BuyerSelectedSignal:
    investor_id: str
    actor_id: str | None = None


@dataclass(slots=True)
class DepositReceivedSignal:
    amount: str  # Decimal as text (JSON-safe)
    received_at: str  # ISO-8601
    actor_id: str | None = None


@dataclass(slots=True)
class DealDeadSignal:
    reason: str
    actor_id: str | None = None


@dataclass(slots=True)
class DealClosedSignal:
    realized_assignment_fee: str
    closed_at: str
    actor_id: str | None = None


DispositionSignal = (
    BuyerResponseSignal
    | BuyerSelectedSignal
    | DepositReceivedSignal
    | DealDeadSignal
    | DealClosedSignal
)


__all__ = [
    "SIGNAL_BOUNCE",
    "SIGNAL_BUYER_RESPONSE",
    "SIGNAL_BUYER_SELECTED",
    "SIGNAL_DEADLINES_UPDATED",
    "SIGNAL_DEAL_CLOSED",
    "SIGNAL_DEAL_DEAD",
    "SIGNAL_DEPOSIT_RECEIVED",
    "SIGNAL_HUMAN_DECISION",
    "SIGNAL_OPT_OUT",
    "SIGNAL_REPLY",
    "BounceSignal",
    "BuyerResponseSignal",
    "BuyerSelectedSignal",
    "DealClosedSignal",
    "DealDeadSignal",
    "DepositReceivedSignal",
    "DispositionSignal",
    "OptOutSignal",
    "ReplySignal",
    "SequenceSignal",
]
