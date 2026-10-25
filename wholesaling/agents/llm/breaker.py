"""Circuit breaker for the model API (REQUIREMENTS.md XC-08).

Closed: calls pass; each failure increments a counter, each success resets it. After
``failure_threshold`` consecutive failures the breaker opens for ``open_seconds``: ``check()``
raises ``CircuitOpen`` without touching the provider. When the window elapses the breaker is
half-open: one call is allowed through; success closes it, failure re-opens it for another
window. State is per process (each worker container has its own breaker), which is what we want:
a worker that keeps failing should stop hammering the API while its siblings may be fine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from wholesaling.agents.llm.interface import CircuitOpen
from wholesaling.logging import get_logger

log = get_logger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            msg = "failure_threshold must be at least 1"
            raise ValueError(msg)
        self.name = name
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self.open_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def check(self) -> None:
        """Raise ``CircuitOpen`` if calls must not proceed right now."""
        state = self.state
        if state is BreakerState.CLOSED:
            return
        if state is BreakerState.HALF_OPEN and not self._probing:
            self._probing = True  # let exactly one probe through
            return
        remaining = 0.0
        if self._opened_at is not None:
            remaining = max(0.0, self.open_seconds - (self._clock() - self._opened_at))
        msg = f"circuit '{self.name}' is open; retry in {remaining:.1f}s"
        raise CircuitOpen(msg)

    def record_success(self) -> None:
        if self._opened_at is not None:
            log.info("llm.breaker.closed", breaker=self.name)
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._failures += 1
        self._probing = False
        if self._opened_at is not None or self._failures >= self.failure_threshold:
            # Re-open (half-open probe failed) or open for the first time.
            self._opened_at = self._clock()
            log.warning(
                "llm.breaker.opened",
                breaker=self.name,
                failures=self._failures,
                open_seconds=self.open_seconds,
            )

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False
