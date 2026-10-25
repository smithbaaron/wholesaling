"""CircuitBreaker state machine (XC-08)."""

from __future__ import annotations

import pytest

from wholesaling.agents.llm.breaker import BreakerState, CircuitBreaker
from wholesaling.agents.llm.interface import CircuitOpen


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_opens_after_threshold_and_half_opens_after_window() -> None:
    clock = Clock()
    breaker = CircuitBreaker(name="t", failure_threshold=3, open_seconds=10, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.check()  # still closed
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitOpen, match="retry in"):
        breaker.check()
    clock.now += 10
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.check()  # one probe allowed
    with pytest.raises(CircuitOpen):
        breaker.check()  # second concurrent probe refused
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failures == 0


def test_failed_probe_reopens_for_another_window() -> None:
    clock = Clock()
    breaker = CircuitBreaker(name="t", failure_threshold=1, open_seconds=5, clock=clock)
    breaker.record_failure()
    clock.now += 5
    breaker.check()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock.now += 4
    with pytest.raises(CircuitOpen):
        breaker.check()


def test_success_resets_failure_count() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


def test_threshold_must_be_positive() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(name="t", failure_threshold=0)
