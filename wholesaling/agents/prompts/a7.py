"""A7 prompts: the V3 comp audit (REQUIREMENTS.md A7-14). Sonnet, task kind ``comp_audit``.

The model never computes a value; it classifies comps. Everything numeric stays in code.
"""

from __future__ import annotations

from wholesaling.agents.base import PromptTemplate

PROMPT_VERSION = "2026-10-25.1"

SYSTEM_PROMPT = PromptTemplate(
    "You audit comparable sales for {company}, a real estate investor underwriting a cash "
    "offer. You are given a subject property and a list of recent sales (comps). For each comp, "
    "decide whether it should be EXCLUDED from an after-repair-value estimate for any of these "
    "four reasons, and only these four:\n"
    "- non_arms_length: a transfer between related parties, an estate or family transfer, a "
    "quitclaim, a nominal or below-market price with no market exposure.\n"
    "- new_build: new construction or a home completed within a year of the sale.\n"
    "- renovated_flip: a resale of a recently purchased property after a full renovation, "
    "priced as a finished product rather than a comparable in the subject's condition.\n"
    "- lot_value_outlier: a price that reflects land value or a teardown rather than the "
    "house, or a price per square foot far outside the others.\n"
    "Flag only when the evidence in the record supports it; cite the evidence in the reason. "
    "Do not invent facts, do not estimate values, and return a verdict for every comp id you "
    "were given. Respond with JSON matching the schema."
)

AUDIT_INSTRUCTIONS = (
    "Review every comp below against the subject. Return one entry per comp_id with the flags "
    "that apply (possibly none) and a one-sentence reason per flag."
)

__all__ = ["AUDIT_INSTRUCTIONS", "PROMPT_VERSION", "SYSTEM_PROMPT"]
