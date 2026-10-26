"""Sheets activities: the one-way projection sync and the daily reconciliation.

- ``sync_projection`` (WF-13): read Postgres, build rows per tab (``integrations.sheets.
  projection``), upsert per tab, record one ``sheets.sync`` audit event. Idempotent: a second
  run against unchanged data writes nothing and reports zero changes; a Temporal retry with the
  same idempotency key returns the recorded counts without touching Sheets again. Tab failures
  are reported per tab, never raised, so one broken tab cannot block the others.
- ``reconcile_sources`` (WF-14, XC-09): compares Outlook (Graph delta over the lookback window)
  with ``messages``, and Postgres with every Sheets tab (missing keys, orphan keys, stale rows).
  It writes **nothing to fix anything**: mismatches become one ``reconciliation.completed``
  audit event plus an ``alert.raised`` event and a structured log alert when any were found.

Dependencies come from ``SheetsActivityContext`` so tests inject the mock gateways and the SQLite
session factory. Postgres is the only writer of truth; nothing here reads a Sheets value back
into the database (STK-02).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from wholesaling.activities.graph import ALERT_EVENT
from wholesaling.config.settings import Settings, get_settings
from wholesaling.db.models.buy_boxes import BuyBox, SearchProfile
from wholesaling.db.models.deals import Match, PropertyCandidate
from wholesaling.db.models.enums import Actor, Channel, Direction
from wholesaling.db.models.investors import Contact, Investor
from wholesaling.db.models.markets import Market
from wholesaling.db.models.messaging import Message, Suppression, Thread
from wholesaling.db.models.ops import QaReview
from wholesaling.db.repositories.audit import AuditRepository
from wholesaling.integrations.graph import GraphGateway, get_graph
from wholesaling.integrations.sheets import (
    TAB_ORDER,
    TAB_SPECS,
    ProjectionSnapshot,
    SheetsGateway,
    build_rows,
    canon_row,
    get_sheets,
)
from wholesaling.integrations.sheets.projection import (
    TAB_BUY_BOXES,
    TAB_CONTACTS,
    TAB_CONVERSATIONS,
    TAB_INVESTORS,
    TAB_MATCHES,
    TAB_OUTREACH_EVENTS,
    TAB_PROPERTIES,
    TAB_QA_LOG,
    TAB_SEARCH_PROFILES,
    TAB_SUPPRESSIONS,
    ConversationStats,
)
from wholesaling.logging import bind_correlation_id, clear_context, get_logger

log = get_logger(__name__)

Clock = Callable[[], datetime]
SYNC_EVENT = "sheets.sync"
RECONCILIATION_EVENT = "reconciliation.completed"
ALERT_KIND_RECONCILIATION = "reconciliation_mismatch"
DEFAULT_LOOKBACK_HOURS = 24
# Cap the mismatch detail kept in the audit payload so a badly drifted sheet cannot bloat a row.
MAX_MISMATCH_SAMPLES = 50


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------------------------
# Dependency context
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SheetsActivityContext:
    sheets: SheetsGateway
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    graph: GraphGateway | None = None
    clock: Clock = _utcnow


_context: SheetsActivityContext | None = None


def set_sheets_context(ctx: SheetsActivityContext | None) -> None:
    """Install (or clear) the dependency context. Tests call this with the mock gateways."""
    global _context
    _context = ctx


def get_sheets_context() -> SheetsActivityContext:
    global _context
    if _context is None:
        from wholesaling.db.engine import get_session_factory

        settings = get_settings()
        _context = SheetsActivityContext(
            sheets=get_sheets(settings),
            session_factory=get_session_factory(),
            settings=settings,
            graph=get_graph(settings),
        )
    return _context


def _bind_workflow_context() -> str | None:
    if activity.in_activity():
        workflow_id = activity.info().workflow_id
        bind_correlation_id(workflow_id)
        return workflow_id
    return None


# --------------------------------------------------------------------------------------------
# Snapshot loading (the only Postgres reads; no writes besides the audit event)
# --------------------------------------------------------------------------------------------


async def load_snapshot(session: AsyncSession, tabs: Sequence[str]) -> ProjectionSnapshot:
    """Load exactly the tables the requested tabs need."""
    wanted = set(tabs)
    snap = ProjectionSnapshot()

    if TAB_INVESTORS in wanted:
        snap.investors = list((await session.execute(select(Investor))).scalars().all())
        markets = (await session.execute(select(Market.id, Market.name))).all()
        snap.market_names = {row[0]: row[1] for row in markets}
    if TAB_CONTACTS in wanted:
        snap.contacts = list((await session.execute(select(Contact))).scalars().all())
    if TAB_BUY_BOXES in wanted or TAB_SEARCH_PROFILES in wanted or TAB_MATCHES in wanted:
        boxes = list((await session.execute(select(BuyBox))).scalars().all())
        snap.buy_box_investor = {b.id: b.investor_id for b in boxes}
        if TAB_BUY_BOXES in wanted:
            snap.buy_boxes = boxes
    if TAB_SEARCH_PROFILES in wanted or TAB_MATCHES in wanted:
        profiles = list((await session.execute(select(SearchProfile))).scalars().all())
        snap.profile_buy_box = {p.id: p.buy_box_id for p in profiles}
        if TAB_SEARCH_PROFILES in wanted:
            snap.search_profiles = profiles
    if TAB_PROPERTIES in wanted:
        snap.properties = list((await session.execute(select(PropertyCandidate))).scalars().all())
    if TAB_MATCHES in wanted:
        snap.matches = list((await session.execute(select(Match))).scalars().all())
    if TAB_CONVERSATIONS in wanted or TAB_OUTREACH_EVENTS in wanted:
        threads = list((await session.execute(select(Thread))).scalars().all())
        snap.thread_investor = {t.id: t.investor_id for t in threads}
        messages = list(
            (await session.execute(select(Message).order_by(Message.created_at, Message.id)))
            .scalars()
            .all()
        )
        if TAB_CONVERSATIONS in wanted:
            snap.threads = threads
            snap.conversation_stats = _conversation_stats(messages)
        if TAB_OUTREACH_EVENTS in wanted:
            snap.messages = messages
    if TAB_SUPPRESSIONS in wanted:
        snap.suppressions = list((await session.execute(select(Suppression))).scalars().all())
    if TAB_QA_LOG in wanted:
        snap.qa_reviews = list((await session.execute(select(QaReview))).scalars().all())
    return snap


def _conversation_stats(messages: Sequence[Message]) -> dict[uuid.UUID, ConversationStats]:
    stats: dict[uuid.UUID, ConversationStats] = {}
    latest: dict[uuid.UUID, datetime] = {}
    for m in messages:
        s = stats.setdefault(m.thread_id, ConversationStats())
        s.message_count += 1
        if m.direction == Direction.IN:
            s.inbound_count += 1
        else:
            s.outbound_count += 1
        at = _aware(m.received_at or m.sent_at or m.created_at) or datetime.min.replace(tzinfo=UTC)
        if m.thread_id not in latest or at >= latest[m.thread_id]:
            latest[m.thread_id] = at
            s.last_direction = str(m.direction.value)
            s.last_classification = m.classification.value if m.classification else None
    return stats


def resolve_tabs(tabs: Sequence[str] | None) -> list[str]:
    if not tabs:
        return list(TAB_ORDER)
    unknown = [t for t in tabs if t not in TAB_SPECS]
    if unknown:
        msg = f"unknown Sheets tab(s) {unknown}; known: {list(TAB_ORDER)}"
        raise ValueError(msg)
    return [t for t in TAB_ORDER if t in set(tabs)]


# --------------------------------------------------------------------------------------------
# sync_projection (WF-13)
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class SyncProjectionInput:
    tabs: list[str] | None = None  # None = every tab in TAB_ORDER
    idempotency_key: str | None = None


@dataclass(slots=True)
class TabSyncResult:
    tab: str
    rows: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    header_written: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def changed(self) -> int:
        return self.created + self.updated


@dataclass(slots=True)
class SyncProjectionResult:
    tabs: list[TabSyncResult]
    tabs_created: list[str] = field(default_factory=list)
    duplicate: bool = False
    spreadsheet_id: str | None = None
    synced_at: str | None = None

    @property
    def ok(self) -> bool:
        return all(t.error is None for t in self.tabs)

    @property
    def failed_tabs(self) -> list[str]:
        return [t.tab for t in self.tabs if t.error is not None]

    @property
    def total_changed(self) -> int:
        return sum(t.created + t.updated for t in self.tabs)


def _result_from_payload(payload: dict[str, Any]) -> SyncProjectionResult:
    tabs = [
        TabSyncResult(
            tab=str(t["tab"]),
            rows=int(t.get("rows", 0)),
            created=int(t.get("created", 0)),
            updated=int(t.get("updated", 0)),
            unchanged=int(t.get("unchanged", 0)),
            header_written=bool(t.get("header_written", False)),
            error=t.get("error"),
        )
        for t in payload.get("tabs", [])
    ]
    return SyncProjectionResult(
        tabs=tabs,
        tabs_created=list(payload.get("tabs_created", [])),
        duplicate=True,
        spreadsheet_id=payload.get("spreadsheet_id"),
        synced_at=payload.get("synced_at"),
    )


@activity.defn(name="sheets_sync_projection")
async def sync_projection(input: SyncProjectionInput) -> SyncProjectionResult:
    """Postgres -> Sheets, one way, per tab, idempotent by row key."""
    workflow_id = _bind_workflow_context()
    ctx = get_sheets_context()
    try:
        tabs = resolve_tabs(input.tabs)
        key = input.idempotency_key or (f"sheets-sync:{workflow_id}" if workflow_id else None)
        async with ctx.session_factory() as session:
            audit = AuditRepository(session)
            if key is not None:
                existing = await audit.get_by_idempotency_key(key)
                if existing is not None:
                    log.info("sheets.sync_duplicate", idempotency_key=key)
                    return _result_from_payload(existing.payload or {})

            snapshot = await load_snapshot(session, tabs)

        specs = [TAB_SPECS[t] for t in tabs]
        tabs_created: list[str] = []
        ensure_error: str | None = None
        try:
            tabs_created = await ctx.sheets.ensure_tabs(specs)
        except Exception as exc:  # reported per tab below, never raised
            ensure_error = f"{type(exc).__name__}: {exc}"
            log.error("sheets.ensure_tabs_failed", error=ensure_error)

        results: list[TabSyncResult] = []
        for tab in tabs:
            spec = TAB_SPECS[tab]
            rows = build_rows(tab, snapshot)
            outcome = TabSyncResult(tab=tab, rows=len(rows))
            if ensure_error is not None:
                outcome.error = ensure_error
                results.append(outcome)
                continue
            try:
                up = await ctx.sheets.upsert_rows(spec, rows)
            except Exception as exc:
                outcome.error = f"{type(exc).__name__}: {exc}"
                log.error("sheets.tab_sync_failed", tab=tab, error=outcome.error)
            else:
                outcome.created, outcome.updated, outcome.unchanged = (
                    up.created,
                    up.updated,
                    up.unchanged,
                )
                outcome.header_written = up.header_written
            results.append(outcome)

        now = ctx.clock()
        result = SyncProjectionResult(
            tabs=results,
            tabs_created=tabs_created,
            spreadsheet_id=ctx.settings.sheets_spreadsheet_id,
            synced_at=now.isoformat(),
        )
        async with ctx.session_factory() as session:
            await AuditRepository(session).record(
                actor=Actor.SYSTEM,
                actor_id="sheets",
                event_type=SYNC_EVENT,
                entity_type="sheets",
                payload={
                    "mode": ctx.sheets.mode,
                    "spreadsheet_id": result.spreadsheet_id,
                    "synced_at": result.synced_at,
                    "tabs_created": tabs_created,
                    "tabs": [asdict(t) for t in results],
                    "ok": result.ok,
                    "failed_tabs": result.failed_tabs,
                    "total_changed": result.total_changed,
                },
                correlation_id=workflow_id,
                idempotency_key=key,
            )
            await session.commit()
        log.info(
            "sheets.sync_completed",
            tabs=len(results),
            changed=result.total_changed,
            failed_tabs=result.failed_tabs,
            tabs_created=tabs_created,
        )
        return result
    finally:
        clear_context()


# --------------------------------------------------------------------------------------------
# reconcile_sources (WF-14 / XC-09)
# --------------------------------------------------------------------------------------------

MISMATCH_OUTLOOK_MISSING_IN_POSTGRES = "outlook_message_missing_in_postgres"
MISMATCH_POSTGRES_MISSING_IN_OUTLOOK = "postgres_message_missing_in_outlook"
MISMATCH_SHEETS_MISSING_KEY = "sheets_missing_key"
MISMATCH_SHEETS_ORPHAN_KEY = "sheets_orphan_key"
MISMATCH_SHEETS_STALE_ROW = "sheets_stale_row"
MISMATCH_SHEETS_TAB_MISSING = "sheets_tab_missing"
MISMATCH_SOURCE_UNAVAILABLE = "source_unavailable"


@dataclass(slots=True)
class Mismatch:
    kind: str
    source: str  # "outlook" | "sheets"
    tab: str | None = None
    key: str | None = None
    detail: str = ""


@dataclass(slots=True)
class ReconcileInput:
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    tabs: list[str] | None = None
    idempotency_key: str | None = None


@dataclass(slots=True)
class ReconcileResult:
    checked_at: str
    lookback_hours: int
    outlook_messages: int = 0
    postgres_inbound_messages: int = 0
    tabs_checked: list[str] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    alerted: bool = False
    duplicate: bool = False

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def count(self, kind: str) -> int:
        return sum(1 for m in self.mismatches if m.kind == kind)


async def _reconcile_outlook(
    ctx: SheetsActivityContext, session: AsyncSession, since: datetime
) -> tuple[list[Mismatch], int, int]:
    mismatches: list[Mismatch] = []
    if ctx.graph is None:
        return (
            [
                Mismatch(
                    kind=MISMATCH_SOURCE_UNAVAILABLE,
                    source="outlook",
                    detail="no Graph gateway configured",
                )
            ],
            0,
            0,
        )
    try:
        page = await ctx.graph.delta_messages(since=since)
    except Exception as exc:
        return (
            [
                Mismatch(
                    kind=MISMATCH_SOURCE_UNAVAILABLE,
                    source="outlook",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            ],
            0,
            0,
        )
    outlook_ids: set[str] = set()
    for m in page.messages:
        at = _aware(m.received_at)
        if at is None or at >= since:
            outlook_ids.add(m.external_message_id)
    stmt = select(Message.external_message_id).where(
        Message.channel == Channel.EMAIL,
        Message.direction == Direction.IN,
        Message.received_at >= since,
    )
    pg_ids = {row[0] for row in (await session.execute(stmt)).all() if row[0]}
    for ext in sorted(outlook_ids - pg_ids):
        mismatches.append(
            Mismatch(
                kind=MISMATCH_OUTLOOK_MISSING_IN_POSTGRES,
                source="outlook",
                key=ext,
                detail="message in the mailbox but not in messages",
            )
        )
    for ext in sorted(pg_ids - outlook_ids):
        mismatches.append(
            Mismatch(
                kind=MISMATCH_POSTGRES_MISSING_IN_OUTLOOK,
                source="outlook",
                key=ext,
                detail="inbound row in messages with no matching mailbox item",
            )
        )
    return mismatches, len(outlook_ids), len(pg_ids)


async def _reconcile_tab(
    ctx: SheetsActivityContext, tab: str, snapshot: ProjectionSnapshot
) -> list[Mismatch]:
    spec = TAB_SPECS[tab]
    expected = {str(r[spec.key_col]): r for r in build_rows(tab, snapshot)}
    try:
        sheet_rows = await ctx.sheets.read_rows(tab)
    except Exception as exc:
        return [
            Mismatch(
                kind=MISMATCH_SHEETS_TAB_MISSING,
                source="sheets",
                tab=tab,
                detail=f"{type(exc).__name__}: {exc}",
            )
        ]
    found: dict[str, Any] = {}
    for row in sheet_rows:
        if spec.key_col < len(row) and row[spec.key_col] not in (None, ""):
            found.setdefault(str(row[spec.key_col]), row)
    out: list[Mismatch] = []
    for key in sorted(set(expected) - set(found)):
        out.append(Mismatch(kind=MISMATCH_SHEETS_MISSING_KEY, source="sheets", tab=tab, key=key))
    for key in sorted(set(found) - set(expected)):
        out.append(Mismatch(kind=MISMATCH_SHEETS_ORPHAN_KEY, source="sheets", tab=tab, key=key))
    for key in sorted(set(expected) & set(found)):
        want = canon_row(expected[key], spec.width)
        have = canon_row(found[key], spec.width)
        if want != have:
            pairs = enumerate(zip(want, have, strict=True))
            cols = [spec.header[i] for i, (a, b) in pairs if a != b]
            out.append(
                Mismatch(
                    kind=MISMATCH_SHEETS_STALE_ROW,
                    source="sheets",
                    tab=tab,
                    key=key,
                    detail="columns differ: " + ", ".join(cols),
                )
            )
    return out


@activity.defn(name="sheets_reconcile_sources")
async def reconcile_sources(input: ReconcileInput) -> ReconcileResult:
    """Compare Outlook, Postgres and Sheets; report, never repair (XC-09)."""
    workflow_id = _bind_workflow_context()
    ctx = get_sheets_context()
    try:
        now = ctx.clock()
        since = now - timedelta(hours=input.lookback_hours)
        tabs = resolve_tabs(input.tabs)
        key = input.idempotency_key or (f"reconciliation:{workflow_id}" if workflow_id else None)
        async with ctx.session_factory() as session:
            audit = AuditRepository(session)
            if key is not None:
                existing = await audit.get_by_idempotency_key(key)
                if existing is not None:
                    payload = existing.payload or {}
                    return ReconcileResult(
                        checked_at=str(payload.get("checked_at", now.isoformat())),
                        lookback_hours=int(payload.get("lookback_hours", input.lookback_hours)),
                        outlook_messages=int(payload.get("outlook_messages", 0)),
                        postgres_inbound_messages=int(payload.get("postgres_inbound_messages", 0)),
                        tabs_checked=list(payload.get("tabs_checked", [])),
                        mismatches=[Mismatch(**m) for m in payload.get("mismatches", [])],
                        alerted=bool(payload.get("alerted", False)),
                        duplicate=True,
                    )
            outlook_mismatches, n_outlook, n_pg = await _reconcile_outlook(ctx, session, since)
            snapshot = await load_snapshot(session, tabs)

        mismatches = list(outlook_mismatches)
        for tab in tabs:
            mismatches.extend(await _reconcile_tab(ctx, tab, snapshot))

        result = ReconcileResult(
            checked_at=now.isoformat(),
            lookback_hours=input.lookback_hours,
            outlook_messages=n_outlook,
            postgres_inbound_messages=n_pg,
            tabs_checked=tabs,
            mismatches=mismatches,
        )
        summary = {kind: result.count(kind) for kind in sorted({m.kind for m in mismatches})}
        async with ctx.session_factory() as session:
            audit = AuditRepository(session)
            await audit.record(
                actor=Actor.SYSTEM,
                actor_id="reconciliation",
                event_type=RECONCILIATION_EVENT,
                entity_type="reconciliation",
                payload={
                    "checked_at": result.checked_at,
                    "lookback_hours": input.lookback_hours,
                    "outlook_messages": n_outlook,
                    "postgres_inbound_messages": n_pg,
                    "tabs_checked": tabs,
                    "mismatch_count": len(mismatches),
                    "summary": summary,
                    "mismatches": [asdict(m) for m in mismatches[:MAX_MISMATCH_SAMPLES]],
                    "truncated": len(mismatches) > MAX_MISMATCH_SAMPLES,
                    "alerted": bool(mismatches),
                },
                correlation_id=workflow_id,
                idempotency_key=key,
            )
            if mismatches:
                log.error(
                    "alert",
                    kind=ALERT_KIND_RECONCILIATION,
                    detail=f"{len(mismatches)} mismatch(es) between Outlook, Postgres and Sheets",
                    summary=summary,
                )
                await audit.record(
                    actor=Actor.SYSTEM,
                    actor_id="reconciliation",
                    event_type=ALERT_EVENT,
                    entity_type="alert",
                    payload={
                        "kind": ALERT_KIND_RECONCILIATION,
                        "detail": (
                            "mismatch between Outlook, Postgres and Sheets; nothing was changed"
                        ),
                        "summary": summary,
                        "mismatch_count": len(mismatches),
                    },
                    correlation_id=workflow_id,
                    idempotency_key=f"{key}:alert" if key else None,
                )
                result.alerted = True
            await session.commit()
        log.info(
            "reconciliation.completed",
            mismatches=len(mismatches),
            summary=summary,
            outlook_messages=n_outlook,
            postgres_inbound_messages=n_pg,
            tabs=len(tabs),
        )
        return result
    finally:
        clear_context()


__all__ = [
    "ALERT_KIND_RECONCILIATION",
    "DEFAULT_LOOKBACK_HOURS",
    "MISMATCH_OUTLOOK_MISSING_IN_POSTGRES",
    "MISMATCH_POSTGRES_MISSING_IN_OUTLOOK",
    "MISMATCH_SHEETS_MISSING_KEY",
    "MISMATCH_SHEETS_ORPHAN_KEY",
    "MISMATCH_SHEETS_STALE_ROW",
    "MISMATCH_SHEETS_TAB_MISSING",
    "MISMATCH_SOURCE_UNAVAILABLE",
    "RECONCILIATION_EVENT",
    "SYNC_EVENT",
    "Mismatch",
    "ReconcileInput",
    "ReconcileResult",
    "SheetsActivityContext",
    "SyncProjectionInput",
    "SyncProjectionResult",
    "TabSyncResult",
    "get_sheets_context",
    "load_snapshot",
    "reconcile_sources",
    "resolve_tabs",
    "set_sheets_context",
    "sync_projection",
]
