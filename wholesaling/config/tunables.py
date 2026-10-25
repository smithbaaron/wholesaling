"""Versioned, non-secret tunables loaded from ``config/*.yaml`` (REQUIREMENTS.md DM-11, ENG-Q6).

Numbers that v2 leaves to "open decisions with defaults" live here, never in code. Each file
is validated into a pydantic model at startup so a bad edit fails fast.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class DealBand(BaseModel):
    min_purchase: Decimal = Decimal("75000")
    max_purchase: Decimal = Decimal("350000")
    min_arv: Decimal = Decimal("120000")
    max_arv: Decimal = Decimal("500000")


class M1Weights(BaseModel):
    demand: float = 0.30
    spread: float = 0.25
    distress: float = 0.20
    liquidity: float = 0.10
    price_fit: float = 0.10
    friction: float = -0.05


class M1Config(BaseModel):
    weights: M1Weights = M1Weights()
    active_cap: int = Field(default=5, ge=1)
    watch_to_active_min_buy_boxes: int = 10
    legal_friction: dict[str, int] = Field(default_factory=dict)  # state -> 0..3
    seed_markets: list[str] = Field(default_factory=list)


class SequenceConfig(BaseModel):
    investor_followup_days: list[int] = [4, 4, 4]  # OPN-04
    seller_followup_days: list[int] = [3, 7, 14]  # OPN-05
    negotiation_reminder_days: list[int] = [2, 5, 10]
    negotiation_expire_day: int = 14
    negotiation_reengage_days: list[int] = [60, 90]
    decline_requeue_days: int = 90


class A7Config(BaseModel):
    assignment_fee: Decimal = Decimal("10000")  # OPN-01
    market_pct: Decimal = Decimal("0.70")  # OPN-02
    negotiation_room_pct: Decimal = Decimal("0.07")  # OPN-03
    closing_cost_pct: Decimal = Decimal("0.03")  # OPN-13 (unvalidated default)
    offer_round_down_to: int = 500
    cash_close_days: int = 21  # OPN-14 (unvalidated default)
    min_offer_pct_of_arv: Decimal = Decimal("0.35")
    asking_tolerance_pct: Decimal = Decimal("0.25")
    repair_rate_per_sqft: dict[str, Decimal] = {  # OPN-12 (unvalidated defaults)
        "light": Decimal("15"),
        "medium": Decimal("30"),
        "heavy": Decimal("50"),
        "gut": Decimal("75"),
    }
    houston_foundation_contingency: Decimal = Decimal("12000")  # OPN-12
    comp_tiers: list[dict[str, Any]] = [
        {"tier": 1, "max_miles": 0.75, "max_days": 180},
        {"tier": 2, "max_miles": 1.0, "max_days": 270},
        {"tier": 3, "max_miles": 1.5, "max_days": 365},
    ]


class CapsConfig(BaseModel):
    l1_daily_investor_sends: int = 50  # OPN-06
    l1_daily_seller_email_sends: int = 200  # OPN-06
    qa_auto_reply_confidence_threshold: float = 0.85  # OPN-10 (proposed)


class Tunables(BaseModel):
    deal_band: DealBand = DealBand()
    m1: M1Config = M1Config()
    sequences: SequenceConfig = SequenceConfig()
    a7: A7Config = A7Config()
    caps: CapsConfig = CapsConfig()


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        msg = f"{path} must contain a mapping at top level"
        raise ValueError(msg)
    return data


@lru_cache(maxsize=1)
def get_tunables(config_dir: Path | None = None) -> Tunables:
    base = config_dir or CONFIG_DIR
    merged: dict[str, Any] = {}
    for section in ("deal_band", "m1", "sequences", "a7", "caps"):
        merged[section] = _load_yaml(base / f"{section}.yaml")
    return Tunables.model_validate(merged)
