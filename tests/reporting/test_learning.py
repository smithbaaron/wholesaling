"""Learning loop on outcomes (BO-13, INV-11, INV-19, M1-05): pure attribution and rate
functions, the read-only loader against seeded rows, the daily activity and its registration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity, workflow

from wholesaling.activities.learning import (
    LEARNING_STATS_EVENT,
    LearningActivityContext,
    LearningInput,
    set_learning_context,
)
from wholesaling.activities.learning import (
    compute_learning_stats as learning_activity,
)
from wholesaling.config.settings import Settings
from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.deals import Contract, ContractStatus, ContractType
from wholesaling.db.models.enums import (
    Channel,
    Direction,
    InvestorType,
    MarketTier,
    MessageClassification,
    OfferStatus,
    PolicyDecision,
)
from wholesaling.db.models.investors import Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Thread
from wholesaling.db.models.offers import Offer
from wholesaling.db.models.ops import AuditEvent
from wholesaling.db.models.sellers import Intake, SellerLead
from wholesaling.reporting.learning import (
    BuyBoxCapture,
    ContractOutcome,
    InboundReply,
    MarketThresholds,
    OfferOutcome,
    OutboundTouch,
    Outcomes,
    attribute_replies,
    compute_learning_stats,
    load_outcomes,
    market_results,
    markets_below_threshold,
    template_stats,
)
from wholesaling.workers.main import ACTIVITIES, WORKFLOWS
from wholesaling.workflows.learning_stats import LearningStatsInput, LearningStatsWorkflow
from wholesaling.workflows.schedules import SCHEDULES, get_schedule

SF = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
WINDOW = (T0 - timedelta(days=1), T0 + timedelta(days=30))


def _touch(
    thread: uuid.UUID,
    hours: float,
    template: str = "initial",
    variant: int | None = 0,
    kind: str = "investor",
    market: str = "Houston, TX",
    step: int = 0,
) -> OutboundTouch:
    return OutboundTouch(
        message_id=uuid.uuid4(),
        thread_id=thread,
        sent_at=T0 + timedelta(hours=hours),
        template_key=template,
        subject_variant=variant,
        kind=kind,
        market=market,
        step=step,
    )


def _reply(
    thread: uuid.UUID, hours: float, cls: MessageClassification | None, auto: bool = False
) -> InboundReply:
    return InboundReply(
        message_id=uuid.uuid4(),
        thread_id=thread,
        received_at=T0 + timedelta(hours=hours),
        classification=cls,
        is_auto_reply=auto,
    )


# -- attribution -------------------------------------------------------------------------------


def test_reply_is_attributed_to_the_latest_touch_before_it() -> None:
    t = uuid.uuid4()
    first, second = _touch(t, 0, step=0), _touch(t, 96, template="followup_1", variant=None, step=1)
    r_after_first = _reply(t, 10, MessageClassification.QUESTION)
    r_after_second = _reply(t, 100, MessageClassification.BUY_BOX_SUPPLIED)
    r_before_all = _reply(t, -5, MessageClassification.INTERESTED)
    by_touch, unattributed = attribute_replies(
        [first, second], [r_after_first, r_after_second, r_before_all]
    )
    assert by_touch[first.message_id] == [r_after_first]
    assert by_touch[second.message_id] == [r_after_second]
    assert unattributed == [r_before_all]


def test_reply_on_a_thread_without_touches_is_unattributed() -> None:
    by_touch, unattributed = attribute_replies(
        [], [_reply(uuid.uuid4(), 1, MessageClassification.INTERESTED)]
    )
    assert by_touch == {} and len(unattributed) == 1


# -- template stats (INV-11 / INV-19) --------------------------------------------------------------


def test_template_stats_per_variant_counts_replies_qualified_and_opt_outs() -> None:
    threads = [uuid.uuid4() for _ in range(6)]
    touches = [
        _touch(threads[0], 0, variant=0),
        _touch(threads[1], 0, variant=0),
        _touch(threads[2], 0, variant=0),
        _touch(threads[3], 0, variant=1),
        _touch(threads[4], 0, variant=1),
        _touch(threads[5], 0, variant=2, market="Columbus, OH"),
    ]
    replies = [
        _reply(threads[0], 5, MessageClassification.BUY_BOX_SUPPLIED),  # qualified
        _reply(threads[1], 5, MessageClassification.OPT_OUT),  # response + opt-out
        _reply(threads[2], 5, MessageClassification.UNRELATED),  # not a response
        _reply(threads[3], 5, MessageClassification.AMBIGUOUS, auto=True),  # OOO never counts
        _reply(threads[4], 5, MessageClassification.INTERESTED),
        _reply(threads[4], 30, MessageClassification.QUESTION),  # second reply, same touch
    ]
    rows = template_stats(Outcomes(*WINDOW, touches=touches, replies=replies))
    by_variant = {r.subject_variant: r for r in rows}
    assert set(by_variant) == {0, 1, 2}
    v0 = by_variant[0]
    assert (v0.sent, v0.replied, v0.qualified_replies, v0.opt_outs) == (3, 2, 1, 1)
    assert v0.reply_rate == 2 / 3 and v0.qualified_rate == 1 / 3 and v0.opt_out_rate == 1 / 3
    v1 = by_variant[1]
    assert (v1.sent, v1.replied, v1.qualified_replies, v1.opt_outs) == (2, 1, 1, 0)
    v2 = by_variant[2]
    assert v2.sent == 1 and v2.replied == 0 and v2.reply_rate == 0.0
    assert v2.markets == ["Columbus, OH"]
    data = v0.as_dict()
    assert data["template_key"] == "initial" and data["reply_rate"] == 2 / 3


def test_template_stats_keep_investor_and_seller_templates_apart() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    touches = [
        _touch(a, 0, template="initial", variant=None, kind="investor"),
        _touch(b, 0, template="initial", variant=None, kind="seller"),
    ]
    rows = template_stats(Outcomes(*WINDOW, touches=touches, replies=[]))
    assert [(r.kind, r.template_key) for r in rows] == [
        ("investor", "initial"),
        ("seller", "initial"),
    ]
    assert all(r.reply_rate == 0.0 for r in rows)


def test_no_touches_gives_no_rows_and_rates_are_none_not_zero_division() -> None:
    assert template_stats(Outcomes(*WINDOW)) == []
    rows = market_results(Outcomes(*WINDOW, buy_boxes=[_capture("Houston, TX")]))
    assert rows[0].reply_rate is None and rows[0].acceptance_rate is None


# -- market results (M1-05) ------------------------------------------------------------------------


def _capture(market: str, *, current: bool = True, active: bool = True) -> BuyBoxCapture:
    return BuyBoxCapture(
        buy_box_id=uuid.uuid4(),
        investor_id=uuid.uuid4(),
        market=market,
        created_at=T0,
        version=1,
        is_current=current,
        is_active=active,
    )


def _offer(market: str, status: OfferStatus) -> OfferOutcome:
    return OfferOutcome(
        offer_id=uuid.uuid4(),
        market=market,
        status=status,
        created_at=T0,
        decided_at=T0,
        offer_price=Decimal("88500"),
        assignment_fee=Decimal("10000"),
    )


def _contract(market: str, status: ContractStatus, fee: str | None) -> ContractOutcome:
    return ContractOutcome(
        contract_id=uuid.uuid4(),
        market=market,
        status=status,
        closed_at=T0 if status is ContractStatus.CLOSED else None,
        realized_assignment_fee=Decimal(fee) if fee else None,
    )


def _two_market_outcomes() -> Outcomes:
    h1, h2, c1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    return Outcomes(
        *WINDOW,
        touches=[
            _touch(h1, 0),
            _touch(h2, 0),
            _touch(c1, 0, market="Columbus, OH", variant=1),
        ],
        replies=[
            _reply(h1, 2, MessageClassification.BUY_BOX_SUPPLIED),
            _reply(c1, 2, MessageClassification.NOT_INTERESTED),
        ],
        buy_boxes=[
            _capture("Houston, TX"),
            _capture("Houston, TX", current=False),  # superseded version: not counted
            _capture("Houston, TX", active=False),  # inactive: not counted
        ],
        offers=[
            _offer("Houston, TX", OfferStatus.ACCEPTED),
            _offer("Houston, TX", OfferStatus.DECLINED),
            _offer("Columbus, OH", OfferStatus.SENT),
        ],
        contracts=[
            _contract("Houston, TX", ContractStatus.CLOSED, "12000"),
            _contract("Houston, TX", ContractStatus.SIGNED, None),
        ],
    )


def test_market_results_realized_per_market() -> None:
    rows = {r.market: r for r in market_results(_two_market_outcomes())}
    h = rows["Houston, TX"]
    assert (h.touches_sent, h.replies, h.qualified_replies) == (2, 1, 1)
    assert h.reply_rate == 0.5 and h.qualified_reply_rate == 0.5
    assert h.buy_boxes_captured == 1 and h.buy_box_versions == 3
    assert (h.offers_made, h.offers_accepted) == (2, 1) and h.acceptance_rate == 0.5
    assert h.assignments_closed == 1 and h.realized_fees_usd == Decimal("12000")
    assert h.below_threshold is None, "OPN-11: no threshold configured, nothing is judged"
    c = rows["Columbus, OH"]
    assert (c.touches_sent, c.replies, c.qualified_replies) == (1, 1, 0)
    assert c.offers_made == 1 and c.offers_accepted == 0 and c.assignments_closed == 0
    assert c.realized_fees_usd == Decimal("0")
    data = h.as_dict()
    assert data["realized_fees_usd"] == "12000" and data["acceptance_rate"] == 0.5


def test_thresholds_are_advisory_and_explain_themselves() -> None:
    thresholds = MarketThresholds(min_reply_rate=0.4, min_buy_boxes=1, min_assignments_closed=1)
    rows = {r.market: r for r in market_results(_two_market_outcomes(), thresholds=thresholds)}
    assert rows["Houston, TX"].below_threshold is False
    assert rows["Houston, TX"].threshold_reasons == []
    columbus = rows["Columbus, OH"]
    assert columbus.below_threshold is True
    assert len(columbus.threshold_reasons) == 2
    assert any("buy boxes 0 < 1" in r for r in columbus.threshold_reasons)
    assert any("assignments closed 0 < 1" in r for r in columbus.threshold_reasons)
    assert markets_below_threshold(rows.values()) == ["Columbus, OH"]
    assert not MarketThresholds().defined


def test_compute_learning_stats_bundles_both_products() -> None:
    outcomes = _two_market_outcomes()
    outcomes.replies.append(_reply(uuid.uuid4(), 1, MessageClassification.INTERESTED))
    stats = compute_learning_stats(outcomes)
    assert [t.subject_variant for t in stats.templates] == [0, 1]
    assert [m.market for m in stats.markets] == ["Columbus, OH", "Houston, TX"]
    assert stats.unattributed_replies == 1
    doc = stats.as_dict()
    assert doc["window_start"] == WINDOW[0].isoformat()
    assert len(doc["templates"]) == 2 and len(doc["markets"]) == 2


# -- loader against seeded rows --------------------------------------------------------------------


async def _seed(session: AsyncSession) -> tuple[Market, Market]:
    houston = Market(name="Houston", state="TX", tier=MarketTier.ACTIVE)
    columbus = Market(name="Columbus", state="OH", tier=MarketTier.WATCH)
    session.add_all([houston, columbus])
    await session.flush()
    investors = []
    for i, market in enumerate([houston, houston, columbus]):
        inv = Investor(
            canonical_name=f"Investor {i} LLC",
            entity_key=f"investor-{i}-llc",
            investor_type=InvestorType.FLIPPER,
            market_id=market.id,
        )
        session.add(inv)
        investors.append(inv)
    await session.flush()
    threads = []
    for i, inv in enumerate(investors):
        t = Thread(
            channel=Channel.EMAIL,
            external_thread_id=f"conv-{i}",
            investor_id=inv.id,
            counterpart_address=f"i{i}@example.com",
        )
        session.add(t)
        threads.append(t)
    await session.flush()

    def msg(thread: Thread, **kw: object) -> Message:
        m = Message(thread_id=thread.id, channel=Channel.EMAIL, **kw)  # type: ignore[arg-type]
        m.external_message_id = f"m-{uuid.uuid4().hex}"
        return m

    # Thread 0 (Houston, variant 0): initial sent, qualified reply, follow-up, OOO after it.
    session.add(
        msg(
            threads[0],
            direction=Direction.OUT,
            draft=False,
            sent_at=T0,
            policy_decision=PolicyDecision.SEND,
            sequence_step=0,
            meta={"template_key": "initial", "subject_variant": 0, "market": "Houston"},
        )
    )
    session.add(
        msg(
            threads[0],
            direction=Direction.IN,
            received_at=T0 + timedelta(hours=3),
            classification=MessageClassification.BUY_BOX_SUPPLIED,
        )
    )
    session.add(
        msg(
            threads[0],
            direction=Direction.OUT,
            draft=False,
            sent_at=T0 + timedelta(days=4),
            sequence_step=1,
            meta={"template_key": "followup_1", "subject_variant": None, "market": "Houston"},
        )
    )
    session.add(
        msg(
            threads[0],
            direction=Direction.IN,
            received_at=T0 + timedelta(days=4, hours=1),
            classification=MessageClassification.AMBIGUOUS,
            meta={"a4": {"is_auto_reply": True}},
        )
    )
    # Thread 1 (Houston, variant 1): sent, opt-out.
    session.add(
        msg(
            threads[1],
            direction=Direction.OUT,
            draft=False,
            sent_at=T0 + timedelta(hours=1),
            sequence_step=0,
            meta={"template_key": "initial", "subject_variant": 1, "market": "Houston"},
        )
    )
    session.add(
        msg(
            threads[1],
            direction=Direction.IN,
            received_at=T0 + timedelta(hours=5),
            classification=MessageClassification.OPT_OUT,
        )
    )
    # Thread 2 (Columbus, variant 2): sent, no reply; plus a queued draft that never went out
    # and a send outside the window.
    session.add(
        msg(
            threads[2],
            direction=Direction.OUT,
            draft=False,
            sent_at=T0 + timedelta(hours=2),
            sequence_step=0,
            meta={"template_key": "initial", "subject_variant": 2, "market": "Columbus"},
        )
    )
    session.add(
        msg(
            threads[2],
            direction=Direction.OUT,
            draft=True,
            policy_decision=PolicyDecision.QUEUE_FOR_HUMAN,
            meta={"template_key": "followup_1", "subject_variant": None},
        )
    )
    session.add(
        msg(
            threads[2],
            direction=Direction.OUT,
            draft=False,
            sent_at=T0 - timedelta(days=40),
            meta={"template_key": "initial", "subject_variant": 2},
        )
    )
    # Buy boxes: v1 superseded + v2 current for investor 0 (Houston); one old one outside.
    bb1 = BuyBox(investor_id=investors[0].id, buy_box_key="default", version=1, is_current=False)
    bb1.created_at = T0 + timedelta(hours=3)
    bb2 = BuyBox(
        investor_id=investors[0].id,
        buy_box_key="default",
        version=2,
        is_current=True,
        unknown_fields=["condition"],
    )
    bb2.created_at = T0 + timedelta(hours=4)
    bb_old = BuyBox(investor_id=investors[2].id, buy_box_key="default", version=1, is_current=True)
    bb_old.created_at = T0 - timedelta(days=60)
    session.add_all([bb1, bb2, bb_old])
    await session.flush()
    # Seller side: Houston lead with an accepted offer and a closed contract; Columbus lead with
    # a sent offer.
    lead_h = SellerLead(market_id=houston.id, property_address_line1="5 Oak St")
    lead_c = SellerLead(market_id=columbus.id, property_address_line1="9 Elm St")
    session.add_all([lead_h, lead_c])
    await session.flush()
    intake_h = Intake(seller_lead_id=lead_h.id)
    intake_c = Intake(seller_lead_id=lead_c.id)
    session.add_all([intake_h, intake_c])
    await session.flush()
    offer_h = Offer(
        seller_lead_id=lead_h.id,
        intake_id=intake_h.id,
        status=OfferStatus.ACCEPTED,
        offer_price=Decimal("88500"),
        assignment_fee=Decimal("10000"),
        idempotency_key="offer-h",
    )
    offer_h.created_at = T0 + timedelta(days=2)
    offer_h.updated_at = T0 + timedelta(days=5)
    offer_c = Offer(
        seller_lead_id=lead_c.id,
        intake_id=intake_c.id,
        status=OfferStatus.SENT,
        offer_price=Decimal("120000"),
        idempotency_key="offer-c",
    )
    offer_c.created_at = T0 + timedelta(days=2)
    offer_c.updated_at = T0 + timedelta(days=2)
    session.add_all([offer_h, offer_c])
    await session.flush()
    contract = Contract(
        offer_id=offer_h.id,
        seller_lead_id=lead_h.id,
        type=ContractType.PSA,
        status=ContractStatus.CLOSED,
        property_address_line1="5 Oak St",
        closed_at=T0 + timedelta(days=20),
        realized_assignment_fee=Decimal("12500"),
    )
    contract.updated_at = T0 + timedelta(days=20)
    session.add(contract)
    await session.commit()
    return houston, columbus


async def test_load_outcomes_reads_the_window_read_only(session: AsyncSession) -> None:
    await _seed(session)
    before = (await session.execute(select(AuditEvent))).scalars().all()
    outcomes = await load_outcomes(session, *WINDOW)
    assert (await session.execute(select(AuditEvent))).scalars().all() == before

    assert len(outcomes.touches) == 4, "drafts and the send outside the window are not touches"
    by_variant = sorted((t.template_key, t.subject_variant, t.market) for t in outcomes.touches)
    assert by_variant == [
        ("followup_1", None, "Houston, TX"),
        ("initial", 0, "Houston, TX"),
        ("initial", 1, "Houston, TX"),
        ("initial", 2, "Columbus, OH"),
    ]
    assert all(t.kind == "investor" for t in outcomes.touches)
    assert len(outcomes.replies) == 3
    assert sum(r.is_auto_reply for r in outcomes.replies) == 1
    assert len(outcomes.buy_boxes) == 2 and {b.version for b in outcomes.buy_boxes} == {1, 2}
    assert [b.unknown_count for b in sorted(outcomes.buy_boxes, key=lambda b: b.version)] == [0, 1]
    assert {(o.market, o.status) for o in outcomes.offers} == {
        ("Houston, TX", OfferStatus.ACCEPTED),
        ("Columbus, OH", OfferStatus.SENT),
    }
    assert len(outcomes.contracts) == 1 and outcomes.contracts[0].is_closed
    assert outcomes.contracts[0].realized_assignment_fee == Decimal("12500")


async def test_stats_from_seeded_rows(session: AsyncSession) -> None:
    await _seed(session)
    stats = compute_learning_stats(await load_outcomes(session, *WINDOW))
    templates = {(t.template_key, t.subject_variant): t for t in stats.templates}
    assert templates[("initial", 0)].qualified_replies == 1
    assert templates[("initial", 0)].reply_rate == 1.0
    assert templates[("initial", 1)].opt_outs == 1 and templates[("initial", 1)].replied == 1
    assert templates[("initial", 2)].replied == 0
    assert templates[("followup_1", None)].replied == 0, "the OOO after the follow-up never counts"
    markets = {m.market: m for m in stats.markets}
    h = markets["Houston, TX"]
    assert (h.touches_sent, h.replies, h.qualified_replies, h.opt_outs) == (3, 2, 1, 1)
    assert h.buy_boxes_captured == 1 and h.buy_box_versions == 2
    assert (h.offers_made, h.offers_accepted, h.assignments_closed) == (1, 1, 1)
    assert h.realized_fees_usd == Decimal("12500")
    c = markets["Columbus, OH"]
    assert (c.touches_sent, c.replies, c.offers_made, c.offers_accepted) == (1, 0, 1, 0)
    assert stats.unattributed_replies == 0


# -- activity, workflow, schedule ------------------------------------------------------------


def _ctx(session_factory: SF) -> LearningActivityContext:
    settings = Settings(_env_file=None, app_env="test", operator_timezone="America/Chicago")  # type: ignore[call-arg]
    return LearningActivityContext(
        session_factory=session_factory,
        settings=settings,
        clock=lambda: T0 + timedelta(days=25),
    )


async def test_activity_persists_one_learning_stats_event_per_day(
    session_factory: SF, session: AsyncSession
) -> None:
    await _seed(session)
    set_learning_context(_ctx(session_factory))
    try:
        result = await learning_activity(
            LearningInput(day="2026-10-26", window_days=90, idempotency_key="learning:test")
        )
        assert not result.duplicate
        assert result.day == "2026-10-26"
        assert result.templates == 4 and result.markets == 2
        # The 90-day window also covers the 40-day-old send and the 60-day-old buy box.
        assert (result.touches, result.replies, result.buy_boxes) == (5, 2, 2)
        assert (result.offers_accepted, result.assignments_closed) == (1, 1)
        assert result.markets_below_threshold == []
        # window: 90 days ending at the start of 2026-10-27 local (05:00 UTC, CDT)
        assert result.window_end == "2026-10-27T05:00:00+00:00"
        assert datetime.fromisoformat(result.window_end) - datetime.fromisoformat(
            result.window_start
        ) == timedelta(days=90)

        again = await learning_activity(
            LearningInput(day="2026-10-26", window_days=90, idempotency_key="learning:test")
        )
        assert again.duplicate and again.templates == 4 and again.offers_accepted == 1

        async with session_factory() as s:
            events = (
                (
                    await s.execute(
                        select(AuditEvent).where(AuditEvent.event_type == LEARNING_STATS_EVENT)
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        payload = events[0].payload
        assert payload["summary"]["buy_boxes"] == 2 and payload["window_days"] == 90
        assert {t["subject_variant"] for t in payload["templates"]} == {0, 1, 2, None}
        assert {m["market"] for m in payload["markets"]} == {"Houston, TX", "Columbus, OH"}
        assert events[0].idempotency_key == "learning:test"
    finally:
        set_learning_context(None)


async def test_activity_on_an_empty_database(session_factory: SF, engine: object) -> None:
    set_learning_context(_ctx(session_factory))
    try:
        result = await learning_activity(LearningInput(day="2026-10-26", idempotency_key="k"))
        assert result.templates == 0 and result.markets == 0 and result.touches == 0
    finally:
        set_learning_context(None)


def test_workflow_definition_and_registration() -> None:
    d = workflow._Definition.from_class(LearningStatsWorkflow)
    assert d is not None and d.name == "LearningStatsWorkflow" and "status" in d.queries
    assert LearningStatsWorkflow in WORKFLOWS
    names = {activity._Definition.from_callable(a).name for a in ACTIVITIES}  # type: ignore[union-attr]
    assert "compute_learning_stats" in names and learning_activity in ACTIVITIES
    assert LearningStatsWorkflow().status() == {"state": "pending", "result": None}
    assert LearningStatsInput().window_days == 90


def test_schedule_entry_is_daily_and_read_only_by_contract() -> None:
    spec = get_schedule("learning-stats")
    assert spec in SCHEDULES
    assert spec.workflow == "LearningStatsWorkflow"
    assert spec.interval == timedelta(days=1) and spec.overlap == "skip"
    assert spec.args == (LearningStatsInput(),)
    assert "M1-05" in spec.requirement and "INV-11" in spec.requirement
    assert "no market tier changes" in spec.note
    assert {"reporting", "learning"} <= spec.tags
