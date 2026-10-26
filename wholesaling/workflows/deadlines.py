"""Deadline-alert schedule for a deal (REQUIREMENTS.md SEL-10 "deadline alerts", WF-06, WF-23).

Pure, deterministic functions over plain values so ``DispositionWorkflow`` can call them inside
workflow code (no I/O, no clock of their own). The decision recorded in docs/agents/disposition.md:
there is **no separate ``DeadlineAlertWorkflow``**. The timers belong to the deal, they must be
cancelled the instant the deal dies or closes, and they move whenever a human corrects a date;
one workflow per contract holding them is simpler and cannot drift from the deal's state. This
module is that workflow's schedule arithmetic.

Three deadlines (``option_period_end``, ``inspection_deadline``, ``closing_date``), three alerts
each: T-3d, T-1d, T-0. An alert already raised (``raised`` keys) is never raised twice; an alert
whose time has passed when the deal is loaded fires once immediately (better late than silent).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

DEADLINE_FIELDS: Final[tuple[str, ...]] = (
    "option_period_end",
    "inspection_deadline",
    "closing_date",
)
# Days before the deadline at which an alert fires; 0 is the deadline itself.
DEFAULT_ALERT_OFFSETS_DAYS: Final[tuple[int, ...]] = (3, 1, 0)


def alert_label(offset_days: int) -> str:
    return "T-0" if offset_days == 0 else f"T-{offset_days}d"


def alert_key(deadline: str, offset_days: int) -> str:
    """Stable id of one alert: ``closing_date:T-1d``. Doubles as the audit idempotency suffix."""
    return f"{deadline}:{alert_label(offset_days)}"


@dataclass(frozen=True, slots=True)
class DeadlineAlert:
    deadline: str
    offset_days: int
    due_at: datetime  # when the alert fires
    deadline_at: datetime  # the deadline itself

    @property
    def key(self) -> str:
        return alert_key(self.deadline, self.offset_days)

    @property
    def label(self) -> str:
        return alert_label(self.offset_days)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def parse_deadlines(values: dict[str, str | None]) -> dict[str, datetime]:
    """ISO strings (as the activity ships them) -> aware datetimes; blanks dropped."""
    out: dict[str, datetime] = {}
    for name in DEADLINE_FIELDS:
        raw = values.get(name)
        if raw:
            out[name] = _aware(datetime.fromisoformat(raw))
    return out


def schedule(
    deadlines: dict[str, datetime],
    *,
    offsets_days: tuple[int, ...] | list[int] = DEFAULT_ALERT_OFFSETS_DAYS,
) -> list[DeadlineAlert]:
    """Every alert for the given deadlines, soonest first."""
    out: list[DeadlineAlert] = []
    for name in DEADLINE_FIELDS:
        when = deadlines.get(name)
        if when is None:
            continue
        when = _aware(when)
        for days in offsets_days:
            out.append(
                DeadlineAlert(
                    deadline=name,
                    offset_days=int(days),
                    due_at=when - timedelta(days=int(days)),
                    deadline_at=when,
                )
            )
    out.sort(key=lambda a: (a.due_at, a.deadline, -a.offset_days))
    return out


def pending(
    deadlines: dict[str, datetime],
    raised: set[str] | frozenset[str],
    *,
    offsets_days: tuple[int, ...] | list[int] = DEFAULT_ALERT_OFFSETS_DAYS,
) -> list[DeadlineAlert]:
    """Alerts not yet raised, soonest first."""
    return [a for a in schedule(deadlines, offsets_days=offsets_days) if a.key not in raised]


def due(
    deadlines: dict[str, datetime],
    raised: set[str] | frozenset[str],
    now: datetime,
    *,
    offsets_days: tuple[int, ...] | list[int] = DEFAULT_ALERT_OFFSETS_DAYS,
) -> list[DeadlineAlert]:
    """Pending alerts whose ``due_at`` is at or before ``now``."""
    now = _aware(now)
    return [a for a in pending(deadlines, raised, offsets_days=offsets_days) if a.due_at <= now]


def next_due(
    deadlines: dict[str, datetime],
    raised: set[str] | frozenset[str],
    now: datetime,
    *,
    offsets_days: tuple[int, ...] | list[int] = DEFAULT_ALERT_OFFSETS_DAYS,
) -> DeadlineAlert | None:
    """The first pending alert strictly in the future (None when nothing is left)."""
    now = _aware(now)
    for a in pending(deadlines, raised, offsets_days=offsets_days):
        if a.due_at > now:
            return a
    return None


__all__ = [
    "DEADLINE_FIELDS",
    "DEFAULT_ALERT_OFFSETS_DAYS",
    "DeadlineAlert",
    "alert_key",
    "alert_label",
    "due",
    "next_due",
    "parse_deadlines",
    "pending",
    "schedule",
]
