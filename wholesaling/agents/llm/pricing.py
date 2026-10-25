"""Per-model price table and cost estimate (REQUIREMENTS.md XC-02 "cost", ENG-Q8).

VERIFIED 2026-10-25 against Anthropic's model overview
(https://platform.claude.com/docs/en/about-claude/models/overview):

- ``claude-haiku-5-5``  : $0.10 / MTok input, $0.50 / MTok output for prompts up to 100k tokens;
                          $0.50 / $2.50 when the prompt exceeds 100k tokens (the higher rate then
                          applies to the whole request). The >100k tier figures were read from a
                          secondary write-up of the pricing page (see docs/agents/llm.md), the
                          overview itself says "From $0.10 / From $0.50".
- ``claude-sonnet-5-5`` : $2 / $10, no long-context premium.
- ``claude-opus-5-5``   : $4 / $20.
- ``claude-fable-5-1``  : $10 / $50.

Base prices only: no batch discount, no prompt-cache accounting (cache reads/writes are not yet
exposed on ``ModelResponse``; when they are, extend ``estimate_cost``). Costs are rounded to
6 decimal places so sub-cent Haiku calls still add up. Note that ``agent_runs.cost_usd`` is
``NUMERIC(12,2)``, so the persisted value is coarser than the estimate (see docs/agents/llm.md).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from wholesaling.logging import get_logger

log = get_logger(__name__)

MTOK = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")

# model id -> (input USD per million tokens, output USD per million tokens)
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00")),
    "claude-fable-5-1": (Decimal("10.00"), Decimal("50.00")),
}

# model id -> (prompt-token threshold, input price, output price) applied to the whole request
# once the prompt is longer than the threshold.
LONG_CONTEXT_PRICES: dict[str, tuple[int, Decimal, Decimal]] = {
    "claude-haiku-5-5": (100_000, Decimal("0.50"), Decimal("2.50")),
}

_warned_unknown: set[str] = set()


def is_known_model(model: str) -> bool:
    return model in PRICES


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """USD cost of one call, or ``None`` (plus one warning per model) for an unknown model.

    Never raises: an unpriced model must not stop an agent run from being logged.
    """
    prices = PRICES.get(model)
    if prices is None:
        if model not in _warned_unknown:
            _warned_unknown.add(model)
            log.warning("llm.pricing.unknown_model", model=model)
        return None
    input_price, output_price = prices
    long_context = LONG_CONTEXT_PRICES.get(model)
    if long_context is not None and input_tokens > long_context[0]:
        input_price, output_price = long_context[1], long_context[2]
    cost = (
        Decimal(max(input_tokens, 0)) * input_price + Decimal(max(output_tokens, 0)) * output_price
    ) / MTOK
    return cost.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP)
