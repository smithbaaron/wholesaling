"""Policy engine contract (XC-01, POL-01..06, RUL-03): same path, the last step changes."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.enums import PolicyDecision, QaVerdict
from wholesaling.db.repositories.policies import PolicyRepository
from wholesaling.policy.actions import POLICY_TABLE, AutonomyLevel, Behaviour, PolicyAction
from wholesaling.policy.engine import PolicyContext, PolicyEngine, evaluate

APPROVED = PolicyContext(qa_verdict=QaVerdict.APPROVE, qa_confidence=Decimal("0.95"))


@pytest.mark.parametrize("action", list(PolicyAction))
def test_l0_never_sends_except_opt_out(action: PolicyAction) -> None:
    ctx = PolicyContext(
        qa_verdict=QaVerdict.APPROVE,
        qa_confidence=Decimal("0.99"),
        sends_today=0,
        initial_was_approved=True,
        sensitive_content=False,
        a7_gates_passed=True,
        within_negotiation_room=True,
        deal_approved=True,
        consent_recorded=True,
    )
    d = evaluate(action, AutonomyLevel.L0, ctx)
    if action is PolicyAction.OPT_OUT_SUPPRESSION:
        assert d.decision is PolicyDecision.SEND
    else:
        assert d.decision is not PolicyDecision.SEND


@pytest.mark.parametrize("level", list(AutonomyLevel))
@pytest.mark.parametrize("action", [PolicyAction.CONTRACT_EXECUTION, PolicyAction.EARNEST_MONEY])
def test_hard_floors_always_human(action: PolicyAction, level: AutonomyLevel) -> None:
    d = evaluate(action, level, APPROVED)
    assert d.decision is PolicyDecision.QUEUE_FOR_HUMAN
    assert "hard floor" in d.reason


@pytest.mark.parametrize("level", list(AutonomyLevel))
def test_suppressed_recipient_blocks_at_every_level(level: AutonomyLevel) -> None:
    ctx = PolicyContext(qa_verdict=QaVerdict.APPROVE, recipient_suppressed=True, sends_today=0)
    d = evaluate(PolicyAction.INVESTOR_INITIAL_OUTREACH, level, ctx)
    assert d.decision is PolicyDecision.BLOCKED


@pytest.mark.parametrize("level", list(AutonomyLevel))
def test_qa_block_blocks_at_every_level(level: AutonomyLevel) -> None:
    ctx = PolicyContext(qa_verdict=QaVerdict.BLOCK, sends_today=0)
    d = evaluate(PolicyAction.INVESTOR_REPLY, level, ctx)
    assert d.decision is PolicyDecision.BLOCKED


def test_l1_initial_outreach_sends_when_qa_approved_and_under_cap() -> None:
    ctx = PolicyContext(qa_verdict=QaVerdict.APPROVE, sends_today=3)
    d = evaluate(PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, ctx, daily_cap=50)
    assert d.sends


def test_l1_initial_outreach_queues_at_cap() -> None:
    ctx = PolicyContext(qa_verdict=QaVerdict.APPROVE, sends_today=50)
    d = evaluate(PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, ctx, daily_cap=50)
    assert d.decision is PolicyDecision.QUEUE_FOR_HUMAN
    assert "cap" in d.reason


def test_l1_initial_outreach_queues_when_send_count_unknown_and_cap_set() -> None:
    ctx = PolicyContext(qa_verdict=QaVerdict.APPROVE)
    d = evaluate(PolicyAction.INVESTOR_INITIAL_OUTREACH, AutonomyLevel.L1, ctx, daily_cap=50)
    assert d.decision is PolicyDecision.QUEUE_FOR_HUMAN


def test_l1_followup_requires_initial_approved() -> None:
    base = PolicyContext(qa_verdict=QaVerdict.APPROVE)
    assert not evaluate(PolicyAction.INVESTOR_FOLLOWUP, AutonomyLevel.L1, base).sends
    base.initial_was_approved = True
    assert evaluate(PolicyAction.INVESTOR_FOLLOWUP, AutonomyLevel.L1, base).sends


def test_l1_reply_requires_confidence_and_no_sensitive_content() -> None:
    ctx = PolicyContext(
        qa_verdict=QaVerdict.APPROVE, qa_confidence=Decimal("0.90"), sensitive_content=False
    )
    assert evaluate(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L1, ctx).sends
    ctx.qa_confidence = Decimal("0.80")
    assert not evaluate(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L1, ctx).sends
    ctx.qa_confidence = Decimal("0.90")
    ctx.sensitive_content = True
    assert not evaluate(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L1, ctx).sends
    ctx.sensitive_content = None  # unknown is conservative
    assert not evaluate(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L1, ctx).sends


def test_custom_threshold_is_honoured() -> None:
    ctx = PolicyContext(
        qa_verdict=QaVerdict.APPROVE, qa_confidence=Decimal("0.80"), sensitive_content=False
    )
    d = evaluate(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L1, ctx, qa_threshold=Decimal("0.75"))
    assert d.sends


def test_rewrite_verdict_queues_even_at_l2() -> None:
    ctx = PolicyContext(qa_verdict=QaVerdict.REWRITE)
    d = evaluate(PolicyAction.INVESTOR_REPLY, AutonomyLevel.L2, ctx)
    assert d.decision is PolicyDecision.QUEUE_FOR_HUMAN


def test_offer_send_l1_requires_all_gates() -> None:
    ctx = PolicyContext(qa_verdict=QaVerdict.APPROVE, a7_gates_passed=False)
    assert not evaluate(PolicyAction.OFFER_SEND, AutonomyLevel.L1, ctx).sends
    ctx.a7_gates_passed = True
    assert evaluate(PolicyAction.OFFER_SEND, AutonomyLevel.L1, ctx).sends


def test_sms_off_at_l0_and_draft_at_l1() -> None:
    ctx = PolicyContext(consent_recorded=True)
    assert evaluate(PolicyAction.SELLER_OUTREACH_SMS, AutonomyLevel.L0, ctx).decision is (
        PolicyDecision.BLOCKED
    )
    assert evaluate(PolicyAction.SELLER_OUTREACH_SMS, AutonomyLevel.L1, ctx).decision is (
        PolicyDecision.QUEUE_FOR_HUMAN
    )


def test_counter_acceptance_within_room_at_l1_and_l2() -> None:
    for level in (AutonomyLevel.L1, AutonomyLevel.L2):
        assert evaluate(
            PolicyAction.COUNTER_ACCEPTANCE, level, PolicyContext(within_negotiation_room=True)
        ).sends
        assert not evaluate(
            PolicyAction.COUNTER_ACCEPTANCE, level, PolicyContext(within_negotiation_room=False)
        ).sends


def test_every_auto_if_row_has_an_evaluator() -> None:
    """A new AUTO_IF row in POLICY_TABLE must get a condition evaluator, not fall through."""
    for action, levels in POLICY_TABLE.items():
        for level, (behaviour, _) in levels.items():
            if behaviour is Behaviour.AUTO_IF:
                d = evaluate(action, level, PolicyContext())
                assert "no condition evaluator" not in d.reason, (action, level)


async def test_engine_reads_live_level_and_threshold(session: AsyncSession) -> None:
    repo = PolicyRepository(session)
    await repo.ensure_seeded()
    engine = PolicyEngine(session)
    ctx = PolicyContext(
        qa_verdict=QaVerdict.APPROVE, qa_confidence=Decimal("0.80"), sensitive_content=False
    )
    d0 = await engine.decide(PolicyAction.INVESTOR_REPLY, ctx)
    assert d0.level is AutonomyLevel.L0 and not d0.sends

    await repo.set_level(
        PolicyAction.INVESTOR_REPLY,
        AutonomyLevel.L1,
        updated_by="test",
        threshold=Decimal("0.75"),
    )
    d1 = await engine.decide(PolicyAction.INVESTOR_REPLY, ctx)
    assert d1.level is AutonomyLevel.L1 and d1.sends
