"""Learning loop on outcomes (REQUIREMENTS.md BO-13 "learning loop on outcomes", INV-11,
INV-19, M1-05).

Pure functions that turn **outcomes** - replies received, buy boxes captured, offers accepted,
assignments closed - into two feedback products:

(a) **per-template / subject-variant reply-rate stats** (INV-11: subject-line experiments are
    judged on relevance and qualified-response rate, never open rates; INV-19: A5 tracks
    qualified buy boxes, response and opt-out rates). A reply is attributed to the *latest*
    outbound touch on its thread sent before it arrived; automatic replies and ``unrelated``
    classifications never count as responses.
(b) **per-market realized results** (M1-05: once live, proxy market signals are replaced by the
    venture's own results per market - buy-box count, reply rate, offers accepted, assignments
    closed - and a market drops to Watch when its 90-day results fall below a threshold). The
    threshold value is OPEN (OPN-11), so ``below_threshold`` is ``None`` until a
    ``MarketThresholds`` is supplied; nothing here changes a market tier.

Nothing here writes. ``load_outcomes`` reads the rows for a window (read-only); the activity in
``activities/learning.py`` persists the result as a ``learning.stats`` audit event once a day
so the digest, M1 and the operator can consume it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.deals import Contract, ContractStatus
from wholesaling.db.models.enums import Direction, MessageClassification, OfferStatus
from wholesaling.db.models.investors import Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Thread
from wholesaling.db.models.offers import Offer
from wholesaling.db.models.sellers import SellerLead
from wholesaling.db.repositories.messages import is_auto_reply

QUALIFIED_REPLY_CLASSES: frozenset[MessageClassification] = frozenset(
    {MessageClassification.INTERESTED, MessageClassification.BUY_BOX_SUPPLIED}
)
"""BO-21 "verified qualified responses": interested or a buy box supplied."""

RESPONSE_CLASSES: frozenset[MessageClassification] = frozenset(
    c for c in MessageClassification if c is not MessageClassification.UNRELATED
)
"""Every class except ``unrelated`` counts as a response (opt-outs are responses, tracked apart)."""

UNKNOWN_MARKET = "(unknown)"
UNKNOWN_TEMPLATE = "(none)"
ACCEPTED_OFFER_STATUSES: frozenset[OfferStatus] = frozenset({OfferStatus.ACCEPTED})


# --------------------------------------------------------------------------------------------
# Outcomes (inputs): plain projections of the rows
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutboundTouch:
    """One sent outbound message with the template that produced it."""

    message_id: uuid.UUID
    thread_id: uuid.UUID
    sent_at: datetime
    template_key: str
    subject_variant: int | None
    kind: str  # "investor" | "seller"
    market: str
    step: int | None = None


@dataclass(frozen=True, slots=True)
class InboundReply:
    message_id: uuid.UUID
    thread_id: uuid.UUID
    received_at: datetime
    classification: MessageClassification | None
    is_auto_reply: bool = False

    @property
    def counts_as_response(self) -> bool:
        return (
            not self.is_auto_reply
            and self.classification is not None
            and self.classification in RESPONSE_CLASSES
        )

    @property
    def is_qualified(self) -> bool:
        return (
            not self.is_auto_reply
            and self.classification is not None
            and self.classification in QUALIFIED_REPLY_CLASSES
        )

    @property
    def is_opt_out(self) -> bool:
        return not self.is_auto_reply and self.classification is MessageClassification.OPT_OUT


@dataclass(frozen=True, slots=True)
class BuyBoxCapture:
    buy_box_id: uuid.UUID
    investor_id: uuid.UUID
    market: str
    created_at: datetime
    version: int
    is_current: bool
    is_active: bool
    unknown_count: int = 0

    @property
    def is_qualified(self) -> bool:
        """A current, active version (a superseded version is never counted twice)."""
        return self.is_current and self.is_active


@dataclass(frozen=True, slots=True)
class OfferOutcome:
    offer_id: uuid.UUID
    market: str
    status: OfferStatus
    created_at: datetime
    decided_at: datetime | None
    offer_price: Decimal | None
    assignment_fee: Decimal | None

    @property
    def is_accepted(self) -> bool:
        return self.status in ACCEPTED_OFFER_STATUSES


@dataclass(frozen=True, slots=True)
class ContractOutcome:
    contract_id: uuid.UUID
    market: str
    status: ContractStatus
    closed_at: datetime | None
    realized_assignment_fee: Decimal | None

    @property
    def is_closed(self) -> bool:
        return self.status is ContractStatus.CLOSED


@dataclass(slots=True)
class Outcomes:
    """Everything the learning loop looks at for one half-open UTC window ``[start, end)``."""

    window_start: datetime
    window_end: datetime
    touches: list[OutboundTouch] = field(default_factory=list)
    replies: list[InboundReply] = field(default_factory=list)
    buy_boxes: list[BuyBoxCapture] = field(default_factory=list)
    offers: list[OfferOutcome] = field(default_factory=list)
    contracts: list[ContractOutcome] = field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


@dataclass(slots=True)
class TemplateStats:
    """INV-11 / INV-19: one row per (template, subject variant, kind)."""

    template_key: str
    subject_variant: int | None
    kind: str
    sent: int = 0
    replied: int = 0
    qualified_replies: int = 0
    opt_outs: int = 0
    markets: list[str] = field(default_factory=list)

    @property
    def reply_rate(self) -> float | None:
        return _rate(self.replied, self.sent)

    @property
    def qualified_rate(self) -> float | None:
        return _rate(self.qualified_replies, self.sent)

    @property
    def opt_out_rate(self) -> float | None:
        return _rate(self.opt_outs, self.sent)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reply_rate"] = self.reply_rate
        data["qualified_rate"] = self.qualified_rate
        data["opt_out_rate"] = self.opt_out_rate
        return data


@dataclass(frozen=True, slots=True)
class MarketThresholds:
    """M1-05 demotion thresholds. Every value is optional; the real numbers are OPN-11."""

    min_reply_rate: float | None = None
    min_buy_boxes: int | None = None
    min_offers_accepted: int | None = None
    min_assignments_closed: int | None = None

    @property
    def defined(self) -> bool:
        return any(
            v is not None
            for v in (
                self.min_reply_rate,
                self.min_buy_boxes,
                self.min_offers_accepted,
                self.min_assignments_closed,
            )
        )


@dataclass(slots=True)
class MarketResults:
    """M1-05: the venture's own results in one market over the window."""

    market: str
    touches_sent: int = 0
    replies: int = 0
    qualified_replies: int = 0
    opt_outs: int = 0
    buy_boxes_captured: int = 0
    buy_box_versions: int = 0
    offers_made: int = 0
    offers_accepted: int = 0
    assignments_closed: int = 0
    realized_fees_usd: Decimal = Decimal("0")
    below_threshold: bool | None = None
    threshold_reasons: list[str] = field(default_factory=list)

    @property
    def reply_rate(self) -> float | None:
        return _rate(self.replies, self.touches_sent)

    @property
    def qualified_reply_rate(self) -> float | None:
        return _rate(self.qualified_replies, self.touches_sent)

    @property
    def acceptance_rate(self) -> float | None:
        return _rate(self.offers_accepted, self.offers_made)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["realized_fees_usd"] = str(self.realized_fees_usd)
        data["reply_rate"] = self.reply_rate
        data["qualified_reply_rate"] = self.qualified_reply_rate
        data["acceptance_rate"] = self.acceptance_rate
        return data


@dataclass(slots=True)
class LearningStats:
    window_start: datetime
    window_end: datetime
    templates: list[TemplateStats]
    markets: list[MarketResults]
    unattributed_replies: int = 0  # replies on threads with no earlier sent touch

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "templates": [t.as_dict() for t in self.templates],
            "markets": [m.as_dict() for m in self.markets],
            "unattributed_replies": self.unattributed_replies,
        }


# --------------------------------------------------------------------------------------------
# Pure computations
# --------------------------------------------------------------------------------------------


def attribute_replies(
    touches: Sequence[OutboundTouch], replies: Sequence[InboundReply]
) -> tuple[dict[uuid.UUID, list[InboundReply]], list[InboundReply]]:
    """Map each reply to the latest touch on its thread sent strictly before it arrived.

    Returns ``(by_touch_message_id, unattributed)``. A reply that precedes every touch on its
    thread (or whose thread has none) is unattributed and left out of every rate.
    """
    by_thread: dict[uuid.UUID, list[OutboundTouch]] = defaultdict(list)
    for t in touches:
        by_thread[t.thread_id].append(t)
    for items in by_thread.values():
        items.sort(key=lambda t: t.sent_at)
    attributed: dict[uuid.UUID, list[InboundReply]] = defaultdict(list)
    unattributed: list[InboundReply] = []
    for r in replies:
        candidates = [t for t in by_thread.get(r.thread_id, []) if t.sent_at < r.received_at]
        if not candidates:
            unattributed.append(r)
            continue
        attributed[candidates[-1].message_id].append(r)
    return dict(attributed), unattributed


def template_stats(outcomes: Outcomes) -> list[TemplateStats]:
    """(a) INV-11: reply / qualified / opt-out rates per template and subject variant."""
    attributed, _ = attribute_replies(outcomes.touches, outcomes.replies)
    rows: dict[tuple[str, int | None, str], TemplateStats] = {}
    for t in outcomes.touches:
        key = (t.template_key or UNKNOWN_TEMPLATE, t.subject_variant, t.kind)
        row = rows.setdefault(
            key,
            TemplateStats(template_key=key[0], subject_variant=t.subject_variant, kind=t.kind),
        )
        row.sent += 1
        if t.market not in row.markets:
            row.markets.append(t.market)
        replies = attributed.get(t.message_id, [])
        if any(r.counts_as_response for r in replies):
            row.replied += 1
        if any(r.is_qualified for r in replies):
            row.qualified_replies += 1
        if any(r.is_opt_out for r in replies):
            row.opt_outs += 1
    for row in rows.values():
        row.markets.sort()

    def order(r: TemplateStats) -> tuple[str, str, int]:
        return (r.kind, r.template_key, -1 if r.subject_variant is None else r.subject_variant)

    return sorted(rows.values(), key=order)


def _apply_thresholds(row: MarketResults, thresholds: MarketThresholds | None) -> None:
    if thresholds is None or not thresholds.defined:
        row.below_threshold = None
        return
    reasons: list[str] = []
    if thresholds.min_reply_rate is not None:
        rate = row.reply_rate
        if rate is None or rate < thresholds.min_reply_rate:
            reasons.append(
                f"reply rate {rate if rate is None else f'{rate:.1%}'} < "
                f"{thresholds.min_reply_rate:.1%}"
            )
    if thresholds.min_buy_boxes is not None and row.buy_boxes_captured < thresholds.min_buy_boxes:
        reasons.append(f"buy boxes {row.buy_boxes_captured} < {thresholds.min_buy_boxes}")
    if (
        thresholds.min_offers_accepted is not None
        and row.offers_accepted < thresholds.min_offers_accepted
    ):
        reasons.append(f"offers accepted {row.offers_accepted} < {thresholds.min_offers_accepted}")
    if (
        thresholds.min_assignments_closed is not None
        and row.assignments_closed < thresholds.min_assignments_closed
    ):
        reasons.append(
            f"assignments closed {row.assignments_closed} < {thresholds.min_assignments_closed}"
        )
    row.threshold_reasons = reasons
    row.below_threshold = bool(reasons)


def market_results(
    outcomes: Outcomes, *, thresholds: MarketThresholds | None = None
) -> list[MarketResults]:
    """(b) M1-05: realized results per market. ``below_threshold`` stays ``None`` without
    thresholds (OPN-11); with them it is advisory - nothing here changes a market tier."""
    attributed, _ = attribute_replies(outcomes.touches, outcomes.replies)
    rows: dict[str, MarketResults] = {}

    def row_for(market: str) -> MarketResults:
        name = market or UNKNOWN_MARKET
        return rows.setdefault(name, MarketResults(market=name))

    for t in outcomes.touches:
        row = row_for(t.market)
        row.touches_sent += 1
        replies = attributed.get(t.message_id, [])
        if any(r.counts_as_response for r in replies):
            row.replies += 1
        if any(r.is_qualified for r in replies):
            row.qualified_replies += 1
        if any(r.is_opt_out for r in replies):
            row.opt_outs += 1
    for b in outcomes.buy_boxes:
        row = row_for(b.market)
        row.buy_box_versions += 1
        if b.is_qualified:
            row.buy_boxes_captured += 1
    for o in outcomes.offers:
        row = row_for(o.market)
        row.offers_made += 1
        if o.is_accepted:
            row.offers_accepted += 1
    for c in outcomes.contracts:
        row = row_for(c.market)
        if c.is_closed:
            row.assignments_closed += 1
            row.realized_fees_usd += c.realized_assignment_fee or Decimal("0")
    for row in rows.values():
        _apply_thresholds(row, thresholds)
    return sorted(rows.values(), key=lambda r: r.market)


def compute_learning_stats(
    outcomes: Outcomes, *, thresholds: MarketThresholds | None = None
) -> LearningStats:
    _, unattributed = attribute_replies(outcomes.touches, outcomes.replies)
    return LearningStats(
        window_start=outcomes.window_start,
        window_end=outcomes.window_end,
        templates=template_stats(outcomes),
        markets=market_results(outcomes, thresholds=thresholds),
        unattributed_replies=len(unattributed),
    )


# --------------------------------------------------------------------------------------------
# Read-only loader
# --------------------------------------------------------------------------------------------


def _market_name(name: str | None, state: str | None) -> str:
    if not name:
        return UNKNOWN_MARKET
    return f"{name}, {state}" if state else name


def _touch_kind(thread: Thread) -> str:
    return "seller" if thread.seller_lead_id is not None else "investor"


def _variant(meta: Mapping[str, Any]) -> int | None:
    raw = meta.get("subject_variant")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def load_outcomes(session: AsyncSession, start: datetime, end: datetime) -> Outcomes:
    """Read the window's touches, replies, buy boxes, offers and contracts. Writes nothing.

    Touches are outbound rows **sent** in the window; replies are inbound rows **received** in
    the window *or* up to the window end on a thread with a touch in the window, so a reply to
    the window's last touch is seen even when the touch sits at the window edge. Buy boxes by
    ``created_at``; offers by ``updated_at`` (the acceptance moment); contracts by ``closed_at``
    or, when still open, ``updated_at``.
    """
    out = Outcomes(window_start=start, window_end=end)

    touch_rows = (
        await session.execute(
            select(Message, Thread, Investor.market_id, SellerLead.market_id)
            .join(Thread, Thread.id == Message.thread_id)
            .outerjoin(Investor, Investor.id == Thread.investor_id)
            .outerjoin(SellerLead, SellerLead.id == Thread.seller_lead_id)
            .where(
                Message.direction == Direction.OUT,
                Message.draft.is_(False),
                Message.sent_at.is_not(None),
                Message.sent_at >= start,
                Message.sent_at < end,
            )
        )
    ).all()
    market_ids: set[uuid.UUID] = set()
    for _m, _t, inv_market, lead_market in touch_rows:
        for mid in (inv_market, lead_market):
            if mid is not None:
                market_ids.add(mid)

    bb_rows = (
        await session.execute(
            select(BuyBox, Investor.market_id)
            .join(Investor, Investor.id == BuyBox.investor_id)
            .where(BuyBox.created_at >= start, BuyBox.created_at < end)
        )
    ).all()
    offer_rows = (
        await session.execute(
            select(Offer, SellerLead.market_id)
            .join(SellerLead, SellerLead.id == Offer.seller_lead_id)
            .where(Offer.updated_at >= start, Offer.updated_at < end)
        )
    ).all()
    contract_rows = (
        await session.execute(
            select(Contract, SellerLead.market_id)
            .join(SellerLead, SellerLead.id == Contract.seller_lead_id)
            .where(
                or_(
                    and_(
                        Contract.closed_at.is_not(None),
                        Contract.closed_at >= start,
                        Contract.closed_at < end,
                    ),
                    and_(
                        Contract.closed_at.is_(None),
                        Contract.updated_at >= start,
                        Contract.updated_at < end,
                    ),
                )
            )
        )
    ).all()
    for _row, mid in [*bb_rows, *offer_rows, *contract_rows]:
        if mid is not None:
            market_ids.add(mid)

    names: dict[uuid.UUID, str] = {}
    if market_ids:
        for market in (
            await session.execute(select(Market).where(Market.id.in_(market_ids)))
        ).scalars():
            names[market.id] = _market_name(market.name, market.state)

    def market_of(mid: uuid.UUID | None, fallback: str | None = None) -> str:
        if mid is not None and mid in names:
            return names[mid]
        return fallback or UNKNOWN_MARKET

    thread_ids: set[uuid.UUID] = set()
    for message, thread, inv_market, lead_market in touch_rows:
        meta = message.meta or {}
        assert message.sent_at is not None
        thread_ids.add(thread.id)
        out.touches.append(
            OutboundTouch(
                message_id=message.id,
                thread_id=thread.id,
                sent_at=message.sent_at,
                template_key=str(meta.get("template_key") or UNKNOWN_TEMPLATE),
                subject_variant=_variant(meta),
                kind=_touch_kind(thread),
                market=market_of(inv_market or lead_market, meta.get("market")),
                step=message.sequence_step,
            )
        )

    reply_filter = and_(
        Message.direction == Direction.IN,
        Message.received_at.is_not(None),
        Message.received_at < end,
    )
    if thread_ids:
        reply_filter = and_(
            reply_filter,
            or_(Message.received_at >= start, Message.thread_id.in_(thread_ids)),
        )
    else:
        reply_filter = and_(reply_filter, Message.received_at >= start)
    for message in (await session.execute(select(Message).where(reply_filter))).scalars():
        assert message.received_at is not None
        out.replies.append(
            InboundReply(
                message_id=message.id,
                thread_id=message.thread_id,
                received_at=message.received_at,
                classification=message.classification,
                is_auto_reply=is_auto_reply(message),
            )
        )

    for bb, mid in bb_rows:
        out.buy_boxes.append(
            BuyBoxCapture(
                buy_box_id=bb.id,
                investor_id=bb.investor_id,
                market=market_of(mid),
                created_at=bb.created_at,
                version=bb.version,
                is_current=bb.is_current,
                is_active=bb.is_active,
                unknown_count=len(bb.unknown_fields or []),
            )
        )
    for offer, mid in offer_rows:
        out.offers.append(
            OfferOutcome(
                offer_id=offer.id,
                market=market_of(mid),
                status=offer.status,
                created_at=offer.created_at,
                decided_at=offer.updated_at,
                offer_price=offer.offer_price,
                assignment_fee=offer.assignment_fee,
            )
        )
    for contract, mid in contract_rows:
        out.contracts.append(
            ContractOutcome(
                contract_id=contract.id,
                market=market_of(mid),
                status=contract.status,
                closed_at=contract.closed_at,
                realized_assignment_fee=contract.realized_assignment_fee,
            )
        )
    return out


def markets_below_threshold(stats: Iterable[MarketResults]) -> list[str]:
    return [m.market for m in stats if m.below_threshold]


__all__ = [
    "ACCEPTED_OFFER_STATUSES",
    "QUALIFIED_REPLY_CLASSES",
    "RESPONSE_CLASSES",
    "UNKNOWN_MARKET",
    "UNKNOWN_TEMPLATE",
    "BuyBoxCapture",
    "ContractOutcome",
    "InboundReply",
    "LearningStats",
    "MarketResults",
    "MarketThresholds",
    "OfferOutcome",
    "OutboundTouch",
    "Outcomes",
    "TemplateStats",
    "attribute_replies",
    "compute_learning_stats",
    "load_outcomes",
    "market_results",
    "markets_below_threshold",
    "template_stats",
]
