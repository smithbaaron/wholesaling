"""KPI queries for the daily digest and the policy page (REQUIREMENTS.md BO-21, INV-19, POL-04).

Every function takes an ``AsyncSession`` and a half-open UTC window ``[start, end)``. Nothing
here writes. Definitions (decisions not spelled out in the requirements are marked *proxy*):

- discovered contacts     ``contacts`` rows created in the window
- messages sent           outbound ``messages`` with ``sent_at`` in the window
- responses               inbound ``messages`` received in the window; *qualified responses*
                          are those classified ``interested`` or ``buy_box_supplied``
- qualified buy boxes     ``buy_boxes`` rows created in the window that are current and active
                          (a superseded version is not counted twice)
- matching properties     ``matches`` rows created in the window
- reviewed deals          ``matches`` whose ``reviewed_at`` is in the window (approved or rejected)
- contracts               *proxy*: ``offers`` that reached ``accepted`` in the window (there is
                          no contract table in this build; contract execution is a hard floor)
- assignment fees         sum of ``offers.assignment_fee`` over those accepted offers
- model spend             sum of ``agent_runs.cost_usd`` in the window
- cost per qualified buyer  model spend / qualified buy boxes (None when there are none)
- queue depth / oldest age  live, not windowed

BO-21 says to optimise on verified qualified responses and completed deals, not open rates, so
no open/click metric exists here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.deals import Match
from wholesaling.db.models.enums import (
    Direction,
    MessageClassification,
    OfferStatus,
    PolicyDecision,
    QaVerdict,
)
from wholesaling.db.models.investors import Contact
from wholesaling.db.models.messaging import Message
from wholesaling.db.models.offers import Offer
from wholesaling.db.models.ops import AgentRun, AuditEvent, QaReview
from wholesaling.db.repositories.review import ReviewRepository
from wholesaling.policy.actions import HARD_FLOOR_ACTIONS, PolicyAction

QUALIFIED_RESPONSE_CLASSES = (
    MessageClassification.INTERESTED,
    MessageClassification.BUY_BOX_SUPPLIED,
)
CONTRACT_STATUSES = (OfferStatus.ACCEPTED,)

# POL-04 thresholds.
PROMOTION_MIN_APPROVED = 50
PROMOTION_MAX_EDIT_RATE = 0.05
PROMOTION_WINDOW_DAYS = 30
# POL-05 demotion signal (7-day edit rate) is reported alongside; nothing acts on it here.
DEMOTION_EDIT_RATE = 0.15
DEMOTION_WINDOW_DAYS = 7
COMPLIANCE_FLAG_MARKERS = ("compliance", "prohibited", "opt_out", "opt-out", "legal")

# Audit event types written by the review queue (api/routes/review.py).
EVENT_APPROVED = "review.approved"
EVENT_APPROVED_WITH_EDITS = "review.approved_with_edits"


def _d(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int((await session.execute(stmt)).scalar_one() or 0)


@dataclass(slots=True)
class KpiSnapshot:
    """The BO-21 counters for one window."""

    window_start: datetime
    window_end: datetime
    discovered_contacts: int = 0
    messages_sent: int = 0
    responses: int = 0
    qualified_responses: int = 0
    opt_outs: int = 0
    qualified_buy_boxes: int = 0
    matching_properties: int = 0
    reviewed_deals: int = 0
    contracts: int = 0
    assignment_fees_usd: Decimal = Decimal("0")
    model_spend_usd: Decimal = Decimal("0")
    qa_reviews: int = 0
    qa_blocked: int = 0
    drafts_queued: int = 0
    drafts_auto_sent: int = 0

    @property
    def cost_per_qualified_buyer_usd(self) -> Decimal | None:
        if self.qualified_buy_boxes == 0:
            return None
        return (self.model_spend_usd / self.qualified_buy_boxes).quantize(Decimal("0.0001"))

    @property
    def response_rate(self) -> float | None:
        return None if self.messages_sent == 0 else self.responses / self.messages_sent

    @property
    def opt_out_rate(self) -> float | None:
        return None if self.messages_sent == 0 else self.opt_outs / self.messages_sent

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["window_start"] = self.window_start.isoformat()
        data["window_end"] = self.window_end.isoformat()
        data["assignment_fees_usd"] = str(self.assignment_fees_usd)
        data["model_spend_usd"] = str(self.model_spend_usd)
        cpq = self.cost_per_qualified_buyer_usd
        data["cost_per_qualified_buyer_usd"] = str(cpq) if cpq is not None else None
        data["response_rate"] = self.response_rate
        data["opt_out_rate"] = self.opt_out_rate
        return data


@dataclass(slots=True)
class QueueStats:
    depth: int
    oldest_age_seconds: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"depth": self.depth, "oldest_age_seconds": self.oldest_age_seconds}


@dataclass(slots=True)
class KpiReport:
    day: date
    timezone: str
    generated_at: datetime
    today: KpiSnapshot
    rolling_7d: KpiSnapshot
    queue: QueueStats
    readiness: list[PromotionReadiness] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "timezone": self.timezone,
            "generated_at": self.generated_at.isoformat(),
            "today": self.today.as_dict(),
            "rolling_7d": self.rolling_7d.as_dict(),
            "queue": self.queue.as_dict(),
            "readiness": [r.as_dict() for r in self.readiness],
        }


# --------------------------------------------------------------------------------------------
# Windowed counters
# --------------------------------------------------------------------------------------------


def day_window(day: date, tz: str) -> tuple[datetime, datetime]:
    """UTC bounds of the local calendar day ``day`` in ``tz``."""
    zone = ZoneInfo(tz)
    start_local = datetime(day.year, day.month, day.day, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def _utc(dt: datetime) -> datetime:
    """Bind aware UTC values: Postgres compares ``timestamptz`` correctly and SQLite drops the
    offset when formatting, which matches how ``utcnow()`` rows were stored."""
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


async def discovered_contacts(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(Contact)
        .where(Contact.created_at >= _utc(start), Contact.created_at < _utc(end)),
    )


async def messages_sent(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.OUT,
            Message.sent_at.is_not(None),
            Message.sent_at >= _utc(start),
            Message.sent_at < _utc(end),
        ),
    )


async def responses(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.IN,
            Message.received_at.is_not(None),
            Message.received_at >= _utc(start),
            Message.received_at < _utc(end),
        ),
    )


async def qualified_responses(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.IN,
            Message.received_at.is_not(None),
            Message.received_at >= _utc(start),
            Message.received_at < _utc(end),
            Message.classification.in_(QUALIFIED_RESPONSE_CLASSES),
        ),
    )


async def opt_outs(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.IN,
            Message.received_at.is_not(None),
            Message.received_at >= _utc(start),
            Message.received_at < _utc(end),
            Message.classification == MessageClassification.OPT_OUT,
        ),
    )


async def qualified_buy_boxes(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(BuyBox)
        .where(
            BuyBox.is_current.is_(True),
            BuyBox.is_active.is_(True),
            BuyBox.created_at >= _utc(start),
            BuyBox.created_at < _utc(end),
        ),
    )


async def matching_properties(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(Match)
        .where(Match.created_at >= _utc(start), Match.created_at < _utc(end)),
    )


async def reviewed_deals(session: AsyncSession, start: datetime, end: datetime) -> int:
    return await _count(
        session,
        select(func.count())
        .select_from(Match)
        .where(
            Match.reviewed_at.is_not(None),
            Match.reviewed_at >= _utc(start),
            Match.reviewed_at < _utc(end),
        ),
    )


async def contracts(session: AsyncSession, start: datetime, end: datetime) -> tuple[int, Decimal]:
    """(accepted offers in window, sum of their assignment fees). Proxy for contracts."""
    stmt = select(func.count(), func.coalesce(func.sum(Offer.assignment_fee), 0)).where(
        Offer.status.in_(CONTRACT_STATUSES),
        Offer.updated_at >= _utc(start),
        Offer.updated_at < _utc(end),
    )
    count, fees = (await session.execute(stmt)).one()
    return int(count or 0), _d(fees)


async def model_spend(session: AsyncSession, start: datetime, end: datetime) -> Decimal:
    stmt = select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(
        AgentRun.created_at >= _utc(start), AgentRun.created_at < _utc(end)
    )
    return _d((await session.execute(stmt)).scalar_one())


async def qa_counts(session: AsyncSession, start: datetime, end: datetime) -> tuple[int, int]:
    """(qa reviews in window, of which verdict = block)."""
    total = await _count(
        session,
        select(func.count())
        .select_from(QaReview)
        .where(QaReview.created_at >= _utc(start), QaReview.created_at < _utc(end)),
    )
    blocked = await _count(
        session,
        select(func.count())
        .select_from(QaReview)
        .where(
            QaReview.created_at >= _utc(start),
            QaReview.created_at < _utc(end),
            QaReview.verdict == QaVerdict.BLOCK,
        ),
    )
    return total, blocked


async def policy_decisions(
    session: AsyncSession, start: datetime, end: datetime
) -> tuple[int, int]:
    """(drafts queued for a human, drafts the policy sent automatically) created in window."""
    queued = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.OUT,
            Message.created_at >= _utc(start),
            Message.created_at < _utc(end),
            Message.policy_decision == PolicyDecision.QUEUE_FOR_HUMAN,
        ),
    )
    auto = await _count(
        session,
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == Direction.OUT,
            Message.created_at >= _utc(start),
            Message.created_at < _utc(end),
            Message.policy_decision == PolicyDecision.SEND,
            Message.sent_at.is_not(None),
        ),
    )
    return queued, auto


async def snapshot(session: AsyncSession, start: datetime, end: datetime) -> KpiSnapshot:
    """All BO-21 counters for one window."""
    contract_count, fees = await contracts(session, start, end)
    qa_total, qa_blocked = await qa_counts(session, start, end)
    queued, auto = await policy_decisions(session, start, end)
    return KpiSnapshot(
        window_start=start,
        window_end=end,
        discovered_contacts=await discovered_contacts(session, start, end),
        messages_sent=await messages_sent(session, start, end),
        responses=await responses(session, start, end),
        qualified_responses=await qualified_responses(session, start, end),
        opt_outs=await opt_outs(session, start, end),
        qualified_buy_boxes=await qualified_buy_boxes(session, start, end),
        matching_properties=await matching_properties(session, start, end),
        reviewed_deals=await reviewed_deals(session, start, end),
        contracts=contract_count,
        assignment_fees_usd=fees,
        model_spend_usd=await model_spend(session, start, end),
        qa_reviews=qa_total,
        qa_blocked=qa_blocked,
        drafts_queued=queued,
        drafts_auto_sent=auto,
    )


async def queue_stats(session: AsyncSession, now: datetime | None = None) -> QueueStats:
    repo = ReviewRepository(session)
    depth = await repo.queue_depth()
    oldest = await repo.oldest_queued_at()
    now = now or datetime.now(tz=UTC)
    age = max((now - oldest).total_seconds(), 0.0) if oldest is not None else None
    return QueueStats(depth=depth, oldest_age_seconds=age)


# --------------------------------------------------------------------------------------------
# POL-04 promotion readiness (read-only; nothing here changes a level)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class PromotionReadiness:
    action: PolicyAction
    window_days: int
    approved_drafts: int
    edited_drafts: int
    qa_blocked_sent: int
    compliance_flags: int
    edit_rate_7d: float | None
    hard_floor: bool

    @property
    def edit_rate(self) -> float | None:
        return None if self.approved_drafts == 0 else self.edited_drafts / self.approved_drafts

    @property
    def criteria(self) -> dict[str, bool]:
        rate = self.edit_rate
        return {
            "approved_drafts": self.approved_drafts >= PROMOTION_MIN_APPROVED,
            "edit_rate": rate is not None and rate <= PROMOTION_MAX_EDIT_RATE,
            "no_qa_blocked_sent": self.qa_blocked_sent == 0,
            "no_compliance_flags": self.compliance_flags == 0,
            "not_hard_floor": not self.hard_floor,
        }

    @property
    def ready(self) -> bool:
        return all(self.criteria.values())

    @property
    def demotion_signal(self) -> bool:
        """POL-05: edit rate > 15 % over 7 days (or any compliance flag)."""
        return (self.edit_rate_7d is not None and self.edit_rate_7d > DEMOTION_EDIT_RATE) or (
            self.compliance_flags > 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "window_days": self.window_days,
            "approved_drafts": self.approved_drafts,
            "edited_drafts": self.edited_drafts,
            "edit_rate": self.edit_rate,
            "edit_rate_7d": self.edit_rate_7d,
            "qa_blocked_sent": self.qa_blocked_sent,
            "compliance_flags": self.compliance_flags,
            "hard_floor": self.hard_floor,
            "criteria": self.criteria,
            "ready": self.ready,
            "demotion_signal": self.demotion_signal,
        }


def is_compliance_flag(flag: str) -> bool:
    lowered = flag.lower()
    return any(marker in lowered for marker in COMPLIANCE_FLAG_MARKERS)


async def _approvals(
    session: AsyncSession, action: PolicyAction, start: datetime, end: datetime
) -> tuple[int, int]:
    """(human-approved drafts, of which approved with edits) for ``action`` in the window.

    Read from the review queue's audit events joined to the message's ``policy_action``.
    """
    base = (
        select(func.count())
        .select_from(AuditEvent)
        .join(Message, Message.id == AuditEvent.entity_id)
        .where(
            AuditEvent.entity_type == "message",
            AuditEvent.created_at >= _utc(start),
            AuditEvent.created_at < _utc(end),
            Message.policy_action == action.value,
        )
    )
    approved = await _count(
        session, base.where(AuditEvent.event_type.in_((EVENT_APPROVED, EVENT_APPROVED_WITH_EDITS)))
    )
    edited = await _count(session, base.where(AuditEvent.event_type == EVENT_APPROVED_WITH_EDITS))
    return approved, edited


async def _qa_blocked_sent(
    session: AsyncSession, action: PolicyAction, start: datetime, end: datetime
) -> int:
    """Messages of ``action`` that were sent in the window despite a QA ``block`` verdict."""
    return await _count(
        session,
        select(func.count())
        .select_from(Message)
        .join(QaReview, QaReview.id == Message.qa_review_id)
        .where(
            Message.policy_action == action.value,
            Message.sent_at.is_not(None),
            Message.sent_at >= _utc(start),
            Message.sent_at < _utc(end),
            QaReview.verdict == QaVerdict.BLOCK,
        ),
    )


async def _compliance_flags(
    session: AsyncSession, action: PolicyAction, start: datetime, end: datetime
) -> int:
    """QA flags that look like compliance findings on ``action`` drafts in the window."""
    stmt = (
        select(QaReview.flags)
        .join(Message, Message.id == QaReview.message_id)
        .where(
            Message.policy_action == action.value,
            QaReview.created_at >= _utc(start),
            QaReview.created_at < _utc(end),
        )
    )
    total = 0
    for (flags,) in (await session.execute(stmt)).all():
        total += sum(1 for f in (flags or []) if isinstance(f, str) and is_compliance_flag(f))
    return total


async def promotion_readiness(
    session: AsyncSession,
    action: PolicyAction,
    *,
    now: datetime | None = None,
    window_days: int = PROMOTION_WINDOW_DAYS,
) -> PromotionReadiness:
    now = now or datetime.now(tz=UTC)
    start = now - timedelta(days=window_days)
    start_7d = now - timedelta(days=DEMOTION_WINDOW_DAYS)
    approved, edited = await _approvals(session, action, start, now)
    approved_7d, edited_7d = await _approvals(session, action, start_7d, now)
    return PromotionReadiness(
        action=action,
        window_days=window_days,
        approved_drafts=approved,
        edited_drafts=edited,
        qa_blocked_sent=await _qa_blocked_sent(session, action, start, now),
        compliance_flags=await _compliance_flags(session, action, start, now),
        edit_rate_7d=(edited_7d / approved_7d) if approved_7d else None,
        hard_floor=action in HARD_FLOOR_ACTIONS,
    )


async def all_promotion_readiness(
    session: AsyncSession, *, now: datetime | None = None, window_days: int = PROMOTION_WINDOW_DAYS
) -> list[PromotionReadiness]:
    return [
        await promotion_readiness(session, action, now=now, window_days=window_days)
        for action in PolicyAction
    ]


# --------------------------------------------------------------------------------------------
# The whole report
# --------------------------------------------------------------------------------------------


async def compute_report(
    session: AsyncSession,
    day: date,
    *,
    tz: str,
    now: datetime | None = None,
    include_readiness: bool = True,
) -> KpiReport:
    """KPIs for local day ``day`` plus the rolling 7 days ending at the end of that day."""
    now = now or datetime.now(tz=UTC)
    start, end = day_window(day, tz)
    today = await snapshot(session, start, end)
    rolling = await snapshot(session, end - timedelta(days=7), end)
    queue = await queue_stats(session, now)
    readiness = await all_promotion_readiness(session, now=now) if include_readiness else []
    return KpiReport(
        day=day,
        timezone=tz,
        generated_at=now,
        today=today,
        rolling_7d=rolling,
        queue=queue,
        readiness=readiness,
    )


__all__ = [
    "COMPLIANCE_FLAG_MARKERS",
    "CONTRACT_STATUSES",
    "DEMOTION_EDIT_RATE",
    "PROMOTION_MAX_EDIT_RATE",
    "PROMOTION_MIN_APPROVED",
    "PROMOTION_WINDOW_DAYS",
    "QUALIFIED_RESPONSE_CLASSES",
    "KpiReport",
    "KpiSnapshot",
    "PromotionReadiness",
    "QueueStats",
    "all_promotion_readiness",
    "compute_report",
    "contracts",
    "day_window",
    "discovered_contacts",
    "is_compliance_flag",
    "matching_properties",
    "messages_sent",
    "model_spend",
    "opt_outs",
    "policy_decisions",
    "promotion_readiness",
    "qa_counts",
    "qualified_buy_boxes",
    "qualified_responses",
    "queue_stats",
    "responses",
    "reviewed_deals",
    "snapshot",
]
