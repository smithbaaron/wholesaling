"""The v2 autonomy policy table, verbatim (REQUIREMENTS.md POL-03a..l, RUL-03).

This module is the contract. The policy engine (build step 7) reads the live level per action from
the ``policies`` table and looks up the behaviour here. Every action starts at L0 (POL-02).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class AutonomyLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class PolicyAction(StrEnum):
    INVESTOR_INITIAL_OUTREACH = "investor_initial_outreach"
    INVESTOR_FOLLOWUP = "investor_followup"
    INVESTOR_REPLY = "investor_reply"
    SELLER_OUTREACH_EMAIL = "seller_outreach_email"
    SELLER_OUTREACH_SMS = "seller_outreach_sms"
    SELLER_INTAKE_REPLY = "seller_intake_reply"
    OFFER_SEND = "offer_send"
    COUNTER_ACCEPTANCE = "counter_acceptance"
    CONTRACT_EXECUTION = "contract_execution"
    EARNEST_MONEY = "earnest_money"
    BUYER_DISPOSITION_BLAST = "buyer_disposition_blast"
    OPT_OUT_SUPPRESSION = "opt_out_suppression"


class Behaviour(StrEnum):
    """What the final step of the shared code path does at a given level."""

    DRAFT = "draft"  # queue_for_human; a human sends
    AUTO = "auto"  # send
    AUTO_IF = "auto_if"  # send only when the row's condition holds; otherwise queue_for_human
    HUMAN = "human"  # never automated; queue_for_human always
    OFF = "off"  # action does not run at all


# (behaviour, condition text from v2). Conditions are evaluated by the policy engine in step 7.
POLICY_TABLE: Final[dict[PolicyAction, dict[AutonomyLevel, tuple[Behaviour, str]]]] = {
    PolicyAction.INVESTOR_INITIAL_OUTREACH: {
        AutonomyLevel.L0: (Behaviour.DRAFT, "human sends"),
        AutonomyLevel.L1: (Behaviour.AUTO_IF, "QA-approved; daily cap"),
        AutonomyLevel.L2: (Behaviour.AUTO, "weekly review"),
    },
    PolicyAction.INVESTOR_FOLLOWUP: {
        AutonomyLevel.L0: (Behaviour.DRAFT, ""),
        AutonomyLevel.L1: (Behaviour.AUTO_IF, "initial was approved"),
        AutonomyLevel.L2: (Behaviour.AUTO, ""),
    },
    PolicyAction.INVESTOR_REPLY: {
        AutonomyLevel.L0: (Behaviour.DRAFT, ""),
        AutonomyLevel.L1: (
            Behaviour.AUTO_IF,
            "QA confidence >= threshold; no price/contract/legal content",
        ),
        AutonomyLevel.L2: (Behaviour.AUTO, ""),
    },
    PolicyAction.SELLER_OUTREACH_EMAIL: {
        AutonomyLevel.L0: (Behaviour.DRAFT, ""),
        AutonomyLevel.L1: (Behaviour.AUTO_IF, "daily cap"),
        AutonomyLevel.L2: (Behaviour.AUTO, ""),
    },
    PolicyAction.SELLER_OUTREACH_SMS: {
        AutonomyLevel.L0: (Behaviour.OFF, ""),
        AutonomyLevel.L1: (Behaviour.DRAFT, "draft-only until 10DLC + consent cleared"),
        AutonomyLevel.L2: (Behaviour.AUTO_IF, "consent record exists"),
    },
    PolicyAction.SELLER_INTAKE_REPLY: {
        AutonomyLevel.L0: (Behaviour.DRAFT, ""),
        AutonomyLevel.L1: (Behaviour.AUTO, ""),
        AutonomyLevel.L2: (Behaviour.AUTO, ""),
    },
    PolicyAction.OFFER_SEND: {
        AutonomyLevel.L0: (Behaviour.DRAFT, ""),
        AutonomyLevel.L1: (Behaviour.AUTO_IF, "all A7 gates pass"),
        AutonomyLevel.L2: (Behaviour.AUTO, ""),
    },
    PolicyAction.COUNTER_ACCEPTANCE: {
        AutonomyLevel.L0: (Behaviour.HUMAN, ""),
        AutonomyLevel.L1: (Behaviour.AUTO_IF, "within negotiation room"),
        AutonomyLevel.L2: (Behaviour.AUTO_IF, "within negotiation room"),
    },
    PolicyAction.CONTRACT_EXECUTION: {
        AutonomyLevel.L0: (Behaviour.HUMAN, "always"),
        AutonomyLevel.L1: (Behaviour.HUMAN, "always"),
        AutonomyLevel.L2: (Behaviour.HUMAN, "always"),
    },
    PolicyAction.EARNEST_MONEY: {
        AutonomyLevel.L0: (Behaviour.HUMAN, "always"),
        AutonomyLevel.L1: (Behaviour.HUMAN, "always"),
        AutonomyLevel.L2: (Behaviour.HUMAN, "always"),
    },
    PolicyAction.BUYER_DISPOSITION_BLAST: {
        AutonomyLevel.L0: (Behaviour.DRAFT, ""),
        AutonomyLevel.L1: (Behaviour.AUTO_IF, "approved deals"),
        AutonomyLevel.L2: (Behaviour.AUTO, ""),
    },
    PolicyAction.OPT_OUT_SUPPRESSION: {
        AutonomyLevel.L0: (Behaviour.AUTO, ""),
        AutonomyLevel.L1: (Behaviour.AUTO, ""),
        AutonomyLevel.L2: (Behaviour.AUTO, ""),
    },
}

# POL-06 hard floors: these can never be promoted above HUMAN.
HARD_FLOOR_ACTIONS: Final[frozenset[PolicyAction]] = frozenset(
    {PolicyAction.CONTRACT_EXECUTION, PolicyAction.EARNEST_MONEY}
)


def default_level() -> AutonomyLevel:
    """POL-02: every action defaults to L0."""
    return AutonomyLevel.L0
