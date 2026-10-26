"""S5 negotiation rules (SEL-09, POL-03h)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wholesaling.agents.s5 import (
    NegotiationSchedule,
    NegotiationState,
    classify_counter,
    counter_policy_context,
)
from wholesaling.config.tunables import SequenceConfig, get_tunables
from wholesaling.db.models.enums import PolicyDecision
from wholesaling.policy.actions import AutonomyLevel, PolicyAction
from wholesaling.policy.engine import evaluate

OFFER = Decimal(88500)
MAO = Decimal("95169.32")


def test_counter_inside_the_room() -> None:
    ev = classify_counter(Decimal(92000), offer=OFFER, mao=MAO)
    assert ev.classification == "within_room" and ev.within_room
    assert ev.to_json()["amount"] == "92000"


def test_counter_at_the_edges() -> None:
    assert classify_counter(OFFER, offer=OFFER, mao=MAO).within_room
    assert classify_counter(MAO, offer=OFFER, mao=MAO).within_room
    above = classify_counter(MAO + Decimal("0.01"), offer=OFFER, mao=MAO)
    assert above.classification == "above_mao" and not above.within_room and "human" in above.reason


def test_counter_below_our_offer_is_inside_the_room() -> None:
    ev = classify_counter(Decimal(80000), offer=OFFER, mao=MAO)
    assert ev.classification == "below_offer" and ev.within_room


def test_counter_must_be_positive() -> None:
    with pytest.raises(ValueError):
        classify_counter(Decimal(0), offer=OFFER, mao=MAO)


@pytest.mark.parametrize(
    ("level", "amount", "expected"),
    [
        (AutonomyLevel.L0, Decimal(92000), PolicyDecision.QUEUE_FOR_HUMAN),  # human at L0
        (AutonomyLevel.L1, Decimal(92000), PolicyDecision.SEND),  # auto within room
        (AutonomyLevel.L1, Decimal(99000), PolicyDecision.QUEUE_FOR_HUMAN),  # above MAO
        (AutonomyLevel.L2, Decimal(95000), PolicyDecision.SEND),
        (AutonomyLevel.L2, Decimal(100000), PolicyDecision.QUEUE_FOR_HUMAN),
    ],
)
def test_policy_table_for_counters(
    level: AutonomyLevel, amount: Decimal, expected: PolicyDecision
) -> None:
    ev = classify_counter(amount, offer=OFFER, mao=MAO)
    ctx = counter_policy_context(ev, seller_suppressed=False)
    assert evaluate(PolicyAction.COUNTER_ACCEPTANCE, level, ctx).decision is expected


def test_suppressed_seller_blocks_even_inside_the_room() -> None:
    ev = classify_counter(Decimal(92000), offer=OFFER, mao=MAO)
    ctx = counter_policy_context(ev, seller_suppressed=True)
    assert (
        evaluate(PolicyAction.COUNTER_ACCEPTANCE, AutonomyLevel.L1, ctx).decision
        is PolicyDecision.BLOCKED
    )


def test_schedule_from_config_matches_sel_09() -> None:
    sched = NegotiationSchedule.from_config(get_tunables().sequences)
    assert sched == NegotiationSchedule((2, 5, 10), 14, (60, 90), 90)
    assert sched.timeline() == [
        (2, "reminder"),
        (5, "reminder"),
        (10, "reminder"),
        (14, "expire"),
        (60, "reengage"),
        (90, "reengage"),
    ]
    assert sched.to_json()["decline_requeue_days"] == 90


def test_schedule_validation() -> None:
    bad = NegotiationSchedule.from_config(
        SequenceConfig(negotiation_reminder_days=[2, 20], negotiation_expire_day=14)
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        bad.timeline()


def test_state_serialises() -> None:
    st = NegotiationState()
    st.counters.append({"amount": "1"})
    assert st.to_json()["status"] == "sent" and st.to_json()["counters"] == [{"amount": "1"}]
