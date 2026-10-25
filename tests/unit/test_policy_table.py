"""The policy table in code must match v2's table row for row (REQUIREMENTS.md POL-03a..l)."""

from __future__ import annotations

from wholesaling.policy.actions import (
    HARD_FLOOR_ACTIONS,
    POLICY_TABLE,
    AutonomyLevel,
    Behaviour,
    PolicyAction,
    default_level,
)

L0, L1, L2 = AutonomyLevel.L0, AutonomyLevel.L1, AutonomyLevel.L2

# Behaviour per level, transcribed from v2 "Autonomy policy".
V2_ROWS: dict[PolicyAction, tuple[Behaviour, Behaviour, Behaviour]] = {
    PolicyAction.INVESTOR_INITIAL_OUTREACH: (Behaviour.DRAFT, Behaviour.AUTO_IF, Behaviour.AUTO),
    PolicyAction.INVESTOR_FOLLOWUP: (Behaviour.DRAFT, Behaviour.AUTO_IF, Behaviour.AUTO),
    PolicyAction.INVESTOR_REPLY: (Behaviour.DRAFT, Behaviour.AUTO_IF, Behaviour.AUTO),
    PolicyAction.SELLER_OUTREACH_EMAIL: (Behaviour.DRAFT, Behaviour.AUTO_IF, Behaviour.AUTO),
    PolicyAction.SELLER_OUTREACH_SMS: (Behaviour.OFF, Behaviour.DRAFT, Behaviour.AUTO_IF),
    PolicyAction.SELLER_INTAKE_REPLY: (Behaviour.DRAFT, Behaviour.AUTO, Behaviour.AUTO),
    PolicyAction.OFFER_SEND: (Behaviour.DRAFT, Behaviour.AUTO_IF, Behaviour.AUTO),
    PolicyAction.COUNTER_ACCEPTANCE: (Behaviour.HUMAN, Behaviour.AUTO_IF, Behaviour.AUTO_IF),
    PolicyAction.CONTRACT_EXECUTION: (Behaviour.HUMAN, Behaviour.HUMAN, Behaviour.HUMAN),
    PolicyAction.EARNEST_MONEY: (Behaviour.HUMAN, Behaviour.HUMAN, Behaviour.HUMAN),
    PolicyAction.BUYER_DISPOSITION_BLAST: (Behaviour.DRAFT, Behaviour.AUTO_IF, Behaviour.AUTO),
    PolicyAction.OPT_OUT_SUPPRESSION: (Behaviour.AUTO, Behaviour.AUTO, Behaviour.AUTO),
}


def test_every_v2_action_is_present_and_nothing_else() -> None:
    assert set(POLICY_TABLE) == set(V2_ROWS) == set(PolicyAction)
    assert len(POLICY_TABLE) == 12


def test_behaviours_match_v2_row_for_row() -> None:
    for action, (b0, b1, b2) in V2_ROWS.items():
        row = POLICY_TABLE[action]
        assert row[L0][0] == b0, (action, "L0")
        assert row[L1][0] == b1, (action, "L1")
        assert row[L2][0] == b2, (action, "L2")


def test_l0_never_sends_except_opt_out_suppression() -> None:
    # POL-02 / RUL-06: at L0 nothing goes out automatically except suppression.
    for action, row in POLICY_TABLE.items():
        behaviour = row[L0][0]
        if action is PolicyAction.OPT_OUT_SUPPRESSION:
            assert behaviour is Behaviour.AUTO
        else:
            assert behaviour in {Behaviour.DRAFT, Behaviour.HUMAN, Behaviour.OFF}, action


def test_hard_floors_are_human_at_every_level() -> None:
    assert {PolicyAction.CONTRACT_EXECUTION, PolicyAction.EARNEST_MONEY} == HARD_FLOOR_ACTIONS
    for action in HARD_FLOOR_ACTIONS:
        for level in AutonomyLevel:
            assert POLICY_TABLE[action][level][0] is Behaviour.HUMAN


def test_default_level_is_l0() -> None:
    assert default_level() is AutonomyLevel.L0
