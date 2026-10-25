"""Price table and cost estimate (XC-02 cost)."""

from __future__ import annotations

from decimal import Decimal

from wholesaling.agents.llm import pricing
from wholesaling.agents.llm.pricing import PRICES, estimate_cost, is_known_model


def test_known_models_match_verified_prices() -> None:
    assert PRICES["claude-haiku-5-5"] == (Decimal("0.10"), Decimal("0.50"))
    assert PRICES["claude-sonnet-5-5"] == (Decimal("2.00"), Decimal("10.00"))


def test_cost_for_sonnet() -> None:
    # 1,000 in + 500 out: 0.002 + 0.005
    assert estimate_cost("claude-sonnet-5-5", 1_000, 500) == Decimal("0.007000")


def test_cost_for_haiku_keeps_sub_cent_precision() -> None:
    assert estimate_cost("claude-haiku-5-5", 2_000, 300) == Decimal("0.000350")


def test_haiku_long_context_tier_applies_to_whole_request() -> None:
    under = estimate_cost("claude-haiku-5-5", 100_000, 1_000)
    over = estimate_cost("claude-haiku-5-5", 100_001, 1_000)
    assert under == Decimal("0.010500")
    assert over == Decimal("0.052501")  # 100001 * 0.5 + 1000 * 2.5, per MTok


def test_unknown_model_returns_none_and_warns_once(caplog) -> None:  # type: ignore[no-untyped-def]
    pricing._warned_unknown.discard("grok-4")
    assert estimate_cost("grok-4", 10, 10) is None
    assert estimate_cost("grok-4", 10, 10) is None
    assert not is_known_model("grok-4")
    assert "grok-4" in pricing._warned_unknown


def test_negative_tokens_are_clamped_not_crashed() -> None:
    assert estimate_cost("claude-sonnet-5-5", -5, 0) == Decimal("0")
