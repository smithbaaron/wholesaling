"""Steps 5-6 (A7-06, A7-07) and V5 (A7-16): MAO / offer arithmetic and the independent recompute."""

from __future__ import annotations

from decimal import Decimal

import pytest

from wholesaling.agents.a7 import mao as mao_mod
from wholesaling.agents.a7.mao import MaoInputs, compute_mao, round_down_to
from wholesaling.agents.a7.recompute import check_against, recompute_from_logged_inputs
from wholesaling.agents.a7.verification import run_v5
from wholesaling.config.tunables import A7Config
from wholesaling.db.models.enums import VerificationResult


def _inputs(a7_config: A7Config, arv: str, repairs: str) -> MaoInputs:
    return MaoInputs.from_config(a7_config, arv=Decimal(arv), repairs=Decimal(repairs))


def test_config_defaults_flow_into_inputs(a7_config: A7Config) -> None:
    inp = _inputs(a7_config, "240161", "60000")
    assert inp.assignment_fee == Decimal(10000)  # OPN-01
    assert inp.market_pct == Decimal("0.70")  # OPN-02
    assert inp.negotiation_room_pct == Decimal("0.07")  # OPN-03
    assert inp.closing_cost_pct == Decimal("0.03")  # OPN-13
    assert inp.offer_round_down_to == 500
    assert MaoInputs.from_json(inp.to_json()) == inp


def test_clean_fixture_numbers(a7_config: A7Config) -> None:
    """ARV 240,161; repairs 60,000: 240,161 x 0.70 = 168,112.70; - 60,000 - 10,000 = 98,112.70;
    closing 3% = 2,943.38; MAO = 95,169.32; offer = 95,169.32 x 0.93 = 88,507.47 -> $88,500."""
    res = compute_mao(_inputs(a7_config, "240161", "60000"))
    assert res.arv_times_pct == Decimal("168112.70")
    assert res.pre_closing == Decimal("98112.70")
    assert res.closing_costs == Decimal("2943.38")
    assert res.mao == Decimal("95169.32")
    assert res.offer_raw == Decimal("88507.47")
    assert res.offer == Decimal(88500)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("88507.47", 88500), ("88999.99", 88500), ("89000.00", 89000), ("499", 0), ("500", 500)],
)
def test_round_down_to_500(raw: str, expected: int) -> None:
    assert round_down_to(Decimal(raw), 500) == Decimal(expected)


def test_round_down_requires_positive_step() -> None:
    with pytest.raises(ValueError):
        round_down_to(Decimal(1000), 0)


def test_negative_mao_gives_zero_offer(a7_config: A7Config) -> None:
    res = compute_mao(_inputs(a7_config, "100000", "90000"))
    assert res.mao < 0 and res.offer == 0


def test_market_pct_and_closing_overrides(a7_config: A7Config) -> None:
    inp = MaoInputs.from_config(
        a7_config,
        arv=Decimal(200000),
        repairs=Decimal(20000),
        market_pct=Decimal("0.75"),
        closing_cost_pct=Decimal("0.02"),
    )
    res = compute_mao(inp)
    assert res.arv_times_pct == Decimal("150000.00")
    assert res.pre_closing == Decimal("120000.00")
    assert res.closing_costs == Decimal("2400.00") and res.mao == Decimal("117600.00")
    assert res.offer == Decimal(109000)  # 109,368 -> 109,000


# -- V5 independent recompute ------------------------------------------------------------------


def test_recompute_matches_engine_on_fixture_numbers(a7_config: A7Config) -> None:
    res = compute_mao(_inputs(a7_config, "240161", "60000"))
    rec = recompute_from_logged_inputs(res.inputs.to_json())
    assert rec.mao_cents == 9516932 and rec.mao_dollars == 95169 and rec.offer_dollars == 88500
    check = check_against(res.inputs.to_json(), engine_mao=res.mao, engine_offer=res.offer)
    assert check.matches and "match" in check.detail


@pytest.mark.parametrize(
    ("arv", "repairs"),
    [
        ("240161", "60000"),
        ("195208", "21000"),
        ("283572", "22500"),
        ("100000", "99999"),
        ("1", "0"),
    ],
)
def test_recompute_agrees_with_mao_across_inputs(
    a7_config: A7Config, arv: str, repairs: str
) -> None:
    res = compute_mao(_inputs(a7_config, arv, repairs))
    ok, record = run_v5(res)
    assert ok and record.result is VerificationResult.PASS
    assert record.inputs["logged_inputs"] == res.inputs.to_json()


def test_recompute_detects_an_off_by_one_in_the_engine(a7_config: A7Config) -> None:
    res = compute_mao(_inputs(a7_config, "240161", "60000"))
    check = check_against(res.inputs.to_json(), engine_mao=res.mao, engine_offer=res.offer + 500)
    assert not check.matches and "offer engine=89000 recompute=88500" in check.detail
    check2 = check_against(res.inputs.to_json(), engine_mao=res.mao + 1, engine_offer=res.offer)
    assert not check2.matches and "MAO engine=95170" in check2.detail


def test_recompute_rejects_non_whole_dollar_offer(a7_config: A7Config) -> None:
    res = compute_mao(_inputs(a7_config, "240161", "60000"))
    check = check_against(
        res.inputs.to_json(), engine_mao=res.mao, engine_offer=Decimal("88500.50")
    )
    assert not check.matches and "whole-dollar" in check.detail


def test_v5_blocks_when_mao_module_has_a_bug(
    a7_config: A7Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a code bug in mao.py (room applied twice): the independent recompute catches it."""
    real = mao_mod.compute_mao

    def buggy(inputs: MaoInputs) -> mao_mod.MaoResult:
        res = real(inputs)
        bad_offer = round_down_to(res.offer_raw * (Decimal(1) - inputs.negotiation_room_pct), 500)
        return mao_mod.MaoResult(
            inputs=res.inputs,
            arv_times_pct=res.arv_times_pct,
            pre_closing=res.pre_closing,
            closing_costs=res.closing_costs,
            mao=res.mao,
            offer_raw=res.offer_raw,
            offer=bad_offer,
        )

    monkeypatch.setattr(mao_mod, "compute_mao", buggy)
    res = mao_mod.compute_mao(_inputs(a7_config, "240161", "60000"))
    assert res.offer == Decimal(82000)
    ok, record = run_v5(res)
    assert not ok and record.result is VerificationResult.FAIL
    assert "BLOCKED" in record.reason and "offer engine=82000 recompute=88500" in record.reason


def test_recompute_does_not_import_mao_module() -> None:
    import inspect

    from wholesaling.agents.a7 import recompute

    src = inspect.getsource(recompute)
    assert "a7.mao" not in src and "from wholesaling.agents.a7 import" not in src
