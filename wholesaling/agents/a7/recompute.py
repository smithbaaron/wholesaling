"""V5 - independent recompute of MAO and offer (REQUIREMENTS.md A7-16).

This module deliberately does NOT import ``mao.py`` or share code with it. It takes the logged
inputs (``MaoInputs.to_json()`` shape: plain strings and an int), re-derives MAO and the offer
with exact rational arithmetic (``fractions.Fraction``) and integer cents, and reports whether
the result matches the engine's numbers to the dollar. A mismatch is a code bug by definition:
the caller blocks the offer and raises an alert; nothing here tries to "fix" the number.

Rounding mirrors the stated formula (half-up to the cent at each step, offer floored to the
configured step) so a correct ``mao.py`` agrees exactly; any drift in either implementation
shows up as a block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

CENTS = 100


def _frac(value: Any) -> Fraction:
    return Fraction(str(value))


def _round_half_up_cents(value: Fraction) -> int:
    """Round a dollar amount (Fraction) to integer cents, halves away from zero."""
    scaled = value * CENTS
    if scaled >= 0:
        return math.floor(scaled + Fraction(1, 2))
    return -math.floor(-scaled + Fraction(1, 2))


def _round_half_up_dollars(cents: int) -> int:
    """Integer cents -> integer dollars, half-up (used to compare MAO to the dollar)."""
    sign = 1 if cents >= 0 else -1
    magnitude = abs(cents)
    return sign * ((magnitude + 50) // 100)


@dataclass(frozen=True, slots=True)
class Recomputed:
    mao_cents: int
    offer_dollars: int

    @property
    def mao_dollars(self) -> int:
        return _round_half_up_dollars(self.mao_cents)

    def to_json(self) -> dict[str, Any]:
        return {
            "mao_cents": self.mao_cents,
            "mao_dollars": self.mao_dollars,
            "offer_dollars": self.offer_dollars,
            "method": "fractions + integer cents (independent of mao.py)",
        }


def recompute_from_logged_inputs(inputs: dict[str, Any]) -> Recomputed:
    """Re-derive MAO and offer from the logged inputs. Pure, exact, dependency-free."""
    arv = _frac(inputs["arv"])
    repairs = _frac(inputs["repairs"])
    fee = _frac(inputs["assignment_fee"])
    market_pct = _frac(inputs["market_pct"])
    closing_pct = _frac(inputs["closing_cost_pct"])
    room_pct = _frac(inputs["negotiation_room_pct"])
    step = int(inputs["offer_round_down_to"])
    if step <= 0:
        msg = "offer_round_down_to must be positive"
        raise ValueError(msg)

    arv_times_pct_c = _round_half_up_cents(arv * market_pct)
    pre_closing_c = arv_times_pct_c - _round_half_up_cents(repairs) - _round_half_up_cents(fee)
    closing_c = _round_half_up_cents(Fraction(pre_closing_c, CENTS) * closing_pct)
    mao_c = pre_closing_c - closing_c
    offer_raw_c = _round_half_up_cents(Fraction(mao_c, CENTS) * (1 - room_pct))
    offer_dollars = 0 if offer_raw_c <= 0 else (offer_raw_c // (step * CENTS)) * step
    return Recomputed(mao_cents=mao_c, offer_dollars=offer_dollars)


@dataclass(frozen=True, slots=True)
class RecomputeCheck:
    matches: bool
    engine_mao_dollars: int
    engine_offer_dollars: int
    recomputed: Recomputed
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {
            "matches": self.matches,
            "engine_mao_dollars": self.engine_mao_dollars,
            "engine_offer_dollars": self.engine_offer_dollars,
            "recomputed": self.recomputed.to_json(),
            "detail": self.detail,
        }


def check_against(inputs: dict[str, Any], *, engine_mao: Any, engine_offer: Any) -> RecomputeCheck:
    """Compare the engine's MAO (to the dollar, half-up) and offer (exact) with the recompute."""
    recomputed = recompute_from_logged_inputs(inputs)
    engine_mao_dollars = _round_half_up_dollars(_round_half_up_cents(_frac(engine_mao)))
    engine_offer_frac = _frac(engine_offer)
    if engine_offer_frac.denominator != 1:
        return RecomputeCheck(
            matches=False,
            engine_mao_dollars=engine_mao_dollars,
            engine_offer_dollars=int(engine_offer_frac),
            recomputed=recomputed,
            detail="engine offer is not a whole-dollar amount",
        )
    engine_offer_dollars = int(engine_offer_frac)
    mao_ok = engine_mao_dollars == recomputed.mao_dollars
    offer_ok = engine_offer_dollars == recomputed.offer_dollars
    if mao_ok and offer_ok:
        detail = "MAO and offer match to the dollar"
    else:
        parts = []
        if not mao_ok:
            parts.append(f"MAO engine={engine_mao_dollars} recompute={recomputed.mao_dollars}")
        if not offer_ok:
            parts.append(
                f"offer engine={engine_offer_dollars} recompute={recomputed.offer_dollars}"
            )
        detail = "MISMATCH: " + "; ".join(parts)
    return RecomputeCheck(
        matches=mao_ok and offer_ok,
        engine_mao_dollars=engine_mao_dollars,
        engine_offer_dollars=engine_offer_dollars,
        recomputed=recomputed,
        detail=detail,
    )


__all__ = ["RecomputeCheck", "Recomputed", "check_against", "recompute_from_logged_inputs"]
