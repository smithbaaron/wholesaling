"""V3 comp audit (A7-14): deterministic pre-filters and the Sonnet-routed model layer."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from tests.agents.a7.conftest import DIRTY
from wholesaling.agents.a7.audit import (
    AUDIT_SCHEMA,
    FLAG_LOT_VALUE_OUTLIER,
    FLAG_NEW_BUILD,
    FLAG_NON_ARMS_LENGTH,
    FLAG_RENOVATED_FLIP,
    MODEL_OVERRIDES,
    A7CompAuditAgent,
    merge_model_verdicts,
    prefilter_flags,
)
from wholesaling.agents.llm.mock import MockModelProvider
from wholesaling.agents.llm.models import TaskKind
from wholesaling.agents.llm.routing import ModelRouter
from wholesaling.agents.llm.runner import AgentRunner
from wholesaling.config.settings import Settings
from wholesaling.db.models.enums import AgentId
from wholesaling.db.models.ops import AgentRun
from wholesaling.integrations.data.property import (
    DEFAULT_TODAY,
    MockPropertyDataProvider,
    SoldComp,
)


def _comp(cid: str, ppsf: int, **kw: object) -> SoldComp:
    base: dict[str, object] = {
        "comp_id": cid,
        "parcel_id": None,
        "address": cid,
        "postal_code": "77055",
        "sale_price": Decimal(ppsf * 1500),
        "sale_date": DEFAULT_TODAY - timedelta(days=30),
        "sqft": 1500,
        "distance_miles": Decimal("0.3"),
        "year_built": 1990,
        "seller_name": f"{cid} Sellerson",
        "buyer_name": f"{cid} Buyerman",
    }
    base.update(kw)
    return SoldComp(**base)  # type: ignore[arg-type]


def test_prefilters_flag_every_dirty_comp_in_the_fixture(
    provider: MockPropertyDataProvider,
) -> None:
    subject = provider.subject(DIRTY)
    assert subject is not None
    verdicts = prefilter_flags(subject.comps)
    flagged = {cid: v.flags for cid, v in verdicts.items() if v.flagged}
    assert flagged == {
        "DC-NAL": [FLAG_NON_ARMS_LENGTH],
        "DC-NEW": [FLAG_NEW_BUILD],
        "DC-FLIP": [FLAG_RENOVATED_FLIP],
        "DC-LOT": [FLAG_LOT_VALUE_OUTLIER],
    }
    assert "share surname 'nguyen'" in verdicts["DC-NAL"].reasons[0]
    assert verdicts["DC-NAL"].sources == ["prefilter"]


def test_new_build_rule_is_year_built_within_one_year_of_sale() -> None:
    sale = DEFAULT_TODAY - timedelta(days=10)
    assert prefilter_flags([_comp("a", 150, year_built=2026, sale_date=sale)])["a"].flags == [
        FLAG_NEW_BUILD
    ]
    assert prefilter_flags([_comp("b", 150, year_built=2025, sale_date=sale)])["b"].flags == [
        FLAG_NEW_BUILD
    ]
    assert prefilter_flags([_comp("c", 150, year_built=2024, sale_date=sale)])["c"].flags == []
    assert prefilter_flags([_comp("d", 150, remarks="New construction home")])["d"].flags == [
        FLAG_NEW_BUILD
    ]


def test_lot_value_outlier_is_2_5x_median() -> None:
    comps = [_comp("a", 100), _comp("b", 110), _comp("c", 120), _comp("hot", 301)]  # median 115
    v = prefilter_flags(comps)
    assert v["hot"].flags == [FLAG_LOT_VALUE_OUTLIER] and v["c"].flags == []
    assert prefilter_flags([_comp("x", 100), _comp("land", 100, remarks="teardown lot")])[
        "land"
    ].flags == [FLAG_LOT_VALUE_OUTLIER]


def test_renovated_flip_needs_recent_prior_sale_plus_cheap_prior_or_remarks() -> None:
    sale = DEFAULT_TODAY - timedelta(days=30)
    cheap = _comp(
        "f1",
        170,
        sale_date=sale,
        prior_sale_date=sale - timedelta(days=200),
        prior_sale_price=Decimal(100000),
    )
    assert prefilter_flags([cheap])["f1"].flags == [FLAG_RENOVATED_FLIP]
    words = _comp(
        "f2",
        170,
        sale_date=sale,
        prior_sale_date=sale - timedelta(days=100),
        prior_sale_price=Decimal(240000),
        remarks="fully renovated",
    )
    assert prefilter_flags([words])["f2"].flags == [FLAG_RENOVATED_FLIP]
    old_prior = _comp(
        "f3",
        170,
        sale_date=sale,
        prior_sale_date=sale - timedelta(days=400),
        prior_sale_price=Decimal(100000),
    )
    assert prefilter_flags([old_prior])["f3"].flags == []
    fair_prior = _comp(
        "f4",
        170,
        sale_date=sale,
        prior_sale_date=sale - timedelta(days=100),
        prior_sale_price=Decimal(240000),
    )
    assert prefilter_flags([fair_prior])["f4"].flags == []


def test_non_arms_length_rules() -> None:
    assert prefilter_flags([_comp("s", 150, seller_name="Ana Perez", buyer_name="Luis Perez")])[
        "s"
    ].flags == [FLAG_NON_ARMS_LENGTH]
    assert (
        prefilter_flags(
            [_comp("e", 150, seller_name="Perez Family Trust", buyer_name="Luis Perez")]
        )["e"].flags
        == []
    )
    assert prefilter_flags([_comp("q", 150, remarks="Quitclaim deed")])["q"].flags == [
        FLAG_NON_ARMS_LENGTH
    ]
    comps = [_comp("a", 150), _comp("b", 152), _comp("c", 148), _comp("cheap", 80)]  # median 149
    assert prefilter_flags(comps)["cheap"].flags == [FLAG_NON_ARMS_LENGTH]


def test_prefilters_on_empty_set() -> None:
    assert prefilter_flags([]) == {}


def test_merge_model_verdicts_adds_known_flags_only() -> None:
    base = prefilter_flags([_comp("a", 150), _comp("b", 150)])
    merged = merge_model_verdicts(
        base,
        {
            "verdicts": [
                {
                    "comp_id": "a",
                    "flags": ["renovated_flip", "bogus"],
                    "reasons": ["remarks say flip"],
                },
                {"comp_id": "zzz", "flags": ["new_build"], "reasons": []},
                "not a dict",
            ],
            "rationale": "x",
        },
    )
    assert merged["a"].flags == [FLAG_RENOVATED_FLIP] and merged["a"].sources == ["model"]
    assert merged["a"].reasons == ["remarks say flip"]
    assert merged["b"].flags == [] and "zzz" not in merged


def test_audit_schema_is_strict() -> None:
    item = AUDIT_SCHEMA["properties"]["verdicts"]["items"]
    assert item["properties"]["flags"]["items"]["enum"] == [
        "non_arms_length",
        "new_build",
        "renovated_flip",
        "lot_value_outlier",
    ]
    assert item["additionalProperties"] is False and AUDIT_SCHEMA["required"] == [
        "verdicts",
        "rationale",
    ]


def test_a7_comp_audit_routes_to_sonnet_via_override(settings: Settings) -> None:
    router = ModelRouter.from_settings(settings, MODEL_OVERRIDES)
    assert router.route(AgentId.A7, TaskKind.COMP_AUDIT) == "claude-sonnet-5-5"
    assert MODEL_OVERRIDES == {"A7": {"comp_audit": "claude-sonnet-5-5"}}


async def test_agent_unions_model_flags_with_prefilters_and_logs_a_run(
    session: AsyncSession, settings: Settings
) -> None:
    mock = MockModelProvider()
    mock.script(
        AgentId.A7,
        TaskKind.COMP_AUDIT,
        parsed={
            "verdicts": [
                {"comp_id": "b", "flags": ["renovated_flip"], "reasons": ["listing says flip"]}
            ],
            "rationale": "one flip",
        },
    )
    runner = AgentRunner(mock, ModelRouter.from_settings(settings, MODEL_OVERRIDES))
    agent = A7CompAuditAgent(runner, session, correlation_id="offer-1")
    comps = [
        _comp("a", 150),
        _comp("b", 152),
        _comp("c", 148),
        _comp("land", 400, remarks="value in land"),
    ]
    result = await agent.audit({"address": "X", "sqft": 1500}, comps, as_of=DEFAULT_TODAY)
    assert set(result.flagged_ids) == {"b", "land"}
    assert result.verdicts["b"].sources == ["model"] and result.verdicts["land"].sources == [
        "prefilter"
    ]
    assert result.model == "claude-sonnet-5-5" and result.run_id is not None
    request = mock.requests[-1]
    assert request.metadata.task_kind is TaskKind.COMP_AUDIT and request.json_schema == AUDIT_SCHEMA
    assert "COMPS:" in request.messages[-1].content and "non_arms_length" in request.system
    run = await session.get(AgentRun, result.run_id)
    assert run is not None and run.agent is AgentId.A7 and run.correlation_id == "offer-1"


async def test_agent_default_mock_response_flags_nothing_beyond_prefilters(
    session: AsyncSession, settings: Settings
) -> None:
    runner = AgentRunner(MockModelProvider(), ModelRouter.from_settings(settings, MODEL_OVERRIDES))
    agent = A7CompAuditAgent(runner, session)
    result = await agent.audit({}, [_comp("a", 150), _comp("b", 151)])
    assert result.flagged_ids == [] and result.run_id is not None
    empty = await agent.audit({}, [])
    assert empty.run_id is None and empty.rationale == "no comps"
