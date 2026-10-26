"""S5 - negotiation rules (REQUIREMENTS.md SEL-09, POL-03h, WF-05).

Pure functions the ``NegotiationWorkflow`` and the offer activities share:

- ``classify_counter``: a seller counter is *within the negotiation room* when
  ``offer <= counter <= MAO`` (a counter at or below our own offer is also inside the room: the
  seller is asking for no more than we would pay); above MAO it is outside and a human decides;
- ``NegotiationSchedule``: reminders at days 2/5/10, expiry at day 14, re-engagement at days
  60/90, declined leads requeued after 90 days (``config/sequences.yaml``);
- the policy context for ``COUNTER_ACCEPTANCE`` (human at L0; auto within room at L1/L2).

Everything numeric lives here or in config; the workflow only sequences timers and signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from wholesaling.config.tunables import SequenceConfig
from wholesaling.policy.engine import PolicyContext

CounterClass = Literal["within_room", "above_mao", "below_offer"]

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True, slots=True)
class CounterEvaluation:
    amount: Decimal
    offer: Decimal
    mao: Decimal
    classification: CounterClass
    within_room: bool
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "amount": str(self.amount),
            "offer": str(self.offer),
            "mao": str(self.mao),
            "classification": self.classification,
            "within_room": self.within_room,
            "reason": self.reason,
        }


def classify_counter(amount: Decimal, *, offer: Decimal, mao: Decimal) -> CounterEvaluation:
    if amount <= 0:
        msg = "counter amount must be positive"
        raise ValueError(msg)
    if amount > mao:
        return CounterEvaluation(
            amount,
            offer,
            mao,
            "above_mao",
            False,
            f"counter {amount} exceeds MAO {mao}: human decides (SEL-09, POL-06)",
        )
    if amount < offer:
        return CounterEvaluation(
            amount,
            offer,
            mao,
            "below_offer",
            True,
            f"counter {amount} is below our offer {offer}: inside the room",
        )
    return CounterEvaluation(
        amount,
        offer,
        mao,
        "within_room",
        True,
        f"counter {amount} is between offer {offer} and MAO {mao}: inside the negotiation room",
    )


def counter_policy_context(
    evaluation: CounterEvaluation, *, seller_suppressed: bool
) -> PolicyContext:
    return PolicyContext(
        within_negotiation_room=evaluation.within_room,
        recipient_suppressed=seller_suppressed,
    )


@dataclass(frozen=True, slots=True)
class NegotiationSchedule:
    reminder_days: tuple[int, ...] = (2, 5, 10)
    expire_day: int = 14
    reengage_days: tuple[int, ...] = (60, 90)
    decline_requeue_days: int = 90

    @classmethod
    def from_config(cls, config: SequenceConfig) -> NegotiationSchedule:
        return cls(
            reminder_days=tuple(int(d) for d in config.negotiation_reminder_days),
            expire_day=int(config.negotiation_expire_day),
            reengage_days=tuple(int(d) for d in config.negotiation_reengage_days),
            decline_requeue_days=int(config.decline_requeue_days),
        )

    def validate(self) -> None:
        days = [*self.reminder_days, self.expire_day, *self.reengage_days]
        if any(d <= 0 for d in days) or days != sorted(days) or len(set(days)) != len(days):
            msg = f"negotiation schedule must be strictly increasing positive days, got {days}"
            raise ValueError(msg)

    def timeline(self) -> list[tuple[int, str]]:
        """``(day, event)`` in order: reminders, expiry, re-engagements."""
        self.validate()
        events = [(d, "reminder") for d in self.reminder_days]
        events.append((self.expire_day, "expire"))
        events.extend((d, "reengage") for d in self.reengage_days)
        return events

    def to_json(self) -> dict[str, Any]:
        return {
            "reminder_days": list(self.reminder_days),
            "expire_day": self.expire_day,
            "reengage_days": list(self.reengage_days),
            "decline_requeue_days": self.decline_requeue_days,
        }


@dataclass(slots=True)
class NegotiationState:
    """Mutable S5 state as the workflow tracks it (mirrored in ``offers.status``)."""

    status: str = "sent"  # sent | countered | accepted | declined | expired | human
    counters: list[dict[str, Any]] = field(default_factory=list)
    reminders_sent: list[int] = field(default_factory=list)
    reengagements_sent: list[int] = field(default_factory=list)
    decline_reason: str | None = None
    accepted_price: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "counters": list(self.counters),
            "reminders_sent": list(self.reminders_sent),
            "reengagements_sent": list(self.reengagements_sent),
            "decline_reason": self.decline_reason,
            "accepted_price": self.accepted_price,
        }


__all__ = [
    "SECONDS_PER_DAY",
    "CounterClass",
    "CounterEvaluation",
    "NegotiationSchedule",
    "NegotiationState",
    "classify_counter",
    "counter_policy_context",
]
