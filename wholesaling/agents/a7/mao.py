"""Steps 5 and 6 - MAO and offer (REQUIREMENTS.md A7-06, A7-07, OPN-01..03, OPN-13).

    pre_closing   = ARV x market_pct - repairs - assignment_fee
    closing_costs = pre_closing x closing_cost_pct          (OPN-13: 3% of the purchase basis)
    MAO           = pre_closing - closing_costs
    offer         = MAO x (1 - negotiation_room_pct), rounded DOWN to $500

OPN-13 says "3% of offer"; the offer depends on MAO, so the percentage is applied to the
pre-closing MAO (the purchase price basis) to avoid a circular definition. Every intermediate
value is kept to the cent and recorded; V5 (``recompute.py``) re-derives MAO and offer from the
same logged inputs with a separate implementation and must agree to the dollar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from wholesaling.config.tunables import A7Config

_CENT = Decimal("0.01")
_DOLLAR = Decimal("1")


@dataclass(frozen=True, slots=True)
class MaoInputs:
    """The logged inputs V5 recomputes from (A7-16). Strings in JSON, Decimals here."""

    arv: Decimal
    repairs: Decimal
    assignment_fee: Decimal
    market_pct: Decimal
    closing_cost_pct: Decimal
    negotiation_room_pct: Decimal
    offer_round_down_to: int

    def to_json(self) -> dict[str, Any]:
        return {
            "arv": str(self.arv),
            "repairs": str(self.repairs),
            "assignment_fee": str(self.assignment_fee),
            "market_pct": str(self.market_pct),
            "closing_cost_pct": str(self.closing_cost_pct),
            "negotiation_room_pct": str(self.negotiation_room_pct),
            "offer_round_down_to": self.offer_round_down_to,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MaoInputs:
        return cls(
            arv=Decimal(str(d["arv"])),
            repairs=Decimal(str(d["repairs"])),
            assignment_fee=Decimal(str(d["assignment_fee"])),
            market_pct=Decimal(str(d["market_pct"])),
            closing_cost_pct=Decimal(str(d["closing_cost_pct"])),
            negotiation_room_pct=Decimal(str(d["negotiation_room_pct"])),
            offer_round_down_to=int(d["offer_round_down_to"]),
        )

    @classmethod
    def from_config(
        cls,
        config: A7Config,
        *,
        arv: Decimal,
        repairs: Decimal,
        market_pct: Decimal | None = None,
        closing_cost_pct: Decimal | None = None,
    ) -> MaoInputs:
        return cls(
            arv=arv,
            repairs=repairs,
            assignment_fee=Decimal(str(config.assignment_fee)),
            market_pct=Decimal(str(market_pct if market_pct is not None else config.market_pct)),
            closing_cost_pct=Decimal(
                str(closing_cost_pct if closing_cost_pct is not None else config.closing_cost_pct)
            ),
            negotiation_room_pct=Decimal(str(config.negotiation_room_pct)),
            offer_round_down_to=int(config.offer_round_down_to),
        )


@dataclass(frozen=True, slots=True)
class MaoResult:
    inputs: MaoInputs
    arv_times_pct: Decimal
    pre_closing: Decimal
    closing_costs: Decimal
    mao: Decimal
    offer_raw: Decimal
    offer: Decimal

    def to_json(self) -> dict[str, Any]:
        return {
            "inputs": self.inputs.to_json(),
            "arv_times_pct": str(self.arv_times_pct),
            "pre_closing": str(self.pre_closing),
            "closing_costs": str(self.closing_costs),
            "mao": str(self.mao),
            "offer_raw": str(self.offer_raw),
            "offer": str(self.offer),
            "formula": (
                "MAO = ARV x market_pct - repairs - assignment_fee - closing_costs; "
                "closing_costs = closing_cost_pct x (ARV x market_pct - repairs - fee); "
                "offer = MAO x (1 - negotiation_room_pct) rounded down to $round_to"
            ),
        }


def round_down_to(value: Decimal, step: int) -> Decimal:
    if step <= 0:
        msg = "offer_round_down_to must be positive"
        raise ValueError(msg)
    units = (value / Decimal(step)).to_integral_value(rounding=ROUND_FLOOR)
    return (units * Decimal(step)).quantize(_DOLLAR)


def compute_mao(inputs: MaoInputs) -> MaoResult:
    """A7-06 + A7-07 from the logged inputs. Negative MAOs are kept (gates reject them)."""
    arv_times_pct = (inputs.arv * inputs.market_pct).quantize(_CENT, rounding=ROUND_HALF_UP)
    pre_closing = arv_times_pct - inputs.repairs - inputs.assignment_fee
    closing = (pre_closing * inputs.closing_cost_pct).quantize(_CENT, rounding=ROUND_HALF_UP)
    mao = (pre_closing - closing).quantize(_CENT)
    offer_raw = (mao * (Decimal(1) - inputs.negotiation_room_pct)).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    offer = round_down_to(offer_raw, inputs.offer_round_down_to) if offer_raw > 0 else Decimal(0)
    return MaoResult(
        inputs=inputs,
        arv_times_pct=arv_times_pct,
        pre_closing=pre_closing,
        closing_costs=closing,
        mao=mao,
        offer_raw=offer_raw,
        offer=offer,
    )


__all__ = ["MaoInputs", "MaoResult", "compute_mao", "round_down_to"]
