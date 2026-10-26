"""``sync_projection`` and ``reconcile_sources`` against seeded SQLite + the mock gateways."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integrations.sheets.conftest import (
    BB_INACTIVE_ID,
    BB_V2_ID,
    INBOUND_EXTERNAL_ID,
    INVESTOR_ID,
    NOW,
    Seed,
    seed_projection,
)
from wholesaling.activities.sheets import (
    ALERT_KIND_RECONCILIATION,
    MISMATCH_OUTLOOK_MISSING_IN_POSTGRES,
    MISMATCH_POSTGRES_MISSING_IN_OUTLOOK,
    MISMATCH_SHEETS_MISSING_KEY,
    MISMATCH_SHEETS_ORPHAN_KEY,
    MISMATCH_SHEETS_STALE_ROW,
    MISMATCH_SHEETS_TAB_MISSING,
    MISMATCH_SOURCE_UNAVAILABLE,
    RECONCILIATION_EVENT,
    SYNC_EVENT,
    ReconcileInput,
    SheetsActivityContext,
    SyncProjectionInput,
    SyncProjectionResult,
    reconcile_sources,
    resolve_tabs,
    set_sheets_context,
    sync_projection,
)
from wholesaling.config.settings import Settings
from wholesaling.db.models import AuditEvent, BuyBox, Investor, Message
from wholesaling.db.models.enums import Direction
from wholesaling.integrations.graph import MockGraphGateway
from wholesaling.integrations.sheets import TAB_ORDER, TAB_SPECS, MockSheetsGateway, row_as_dict
from wholesaling.integrations.sheets.projection import (
    STATUS_INACTIVE,
    TAB_BUY_BOXES,
    TAB_INVESTORS,
    TAB_OUTREACH_EVENTS,
)

SF = async_sessionmaker[AsyncSession]
MAILBOX = "deals@example.test"


@pytest.fixture
def sheets() -> MockSheetsGateway:
    return MockSheetsGateway(spreadsheet_id="sheet-1")


@pytest.fixture
def graph() -> MockGraphGateway:
    return MockGraphGateway(mailbox=MAILBOX, clock=lambda: NOW)


@pytest.fixture
def ctx(
    session_factory: SF, sheets: MockSheetsGateway, graph: MockGraphGateway
) -> SheetsActivityContext:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, app_env="test", sheets_spreadsheet_id="sheet-1", graph_mailbox=MAILBOX
    )
    c = SheetsActivityContext(
        sheets=sheets,
        session_factory=session_factory,
        settings=settings,
        graph=graph,
        clock=lambda: NOW,
    )
    set_sheets_context(c)
    yield c  # type: ignore[misc]
    set_sheets_context(None)


@pytest.fixture
async def seed(session: AsyncSession) -> Seed:
    return await seed_projection(session)


async def _events(sf: SF, event_type: str) -> list[AuditEvent]:
    async with sf() as s:
        stmt = select(AuditEvent).where(AuditEvent.event_type == event_type)
        return list((await s.execute(stmt)).scalars().all())


async def _audit_count(sf: SF) -> int:
    async with sf() as s:
        return int((await s.execute(select(func.count(AuditEvent.id)))).scalar_one())


# --------------------------------------------------------------------------------------------
# sync_projection
# --------------------------------------------------------------------------------------------


def test_resolve_tabs_defaults_to_all_in_order_and_rejects_unknown() -> None:
    assert resolve_tabs(None) == list(TAB_ORDER)
    assert resolve_tabs(["Matches", "Investors"]) == ["Investors", "Matches"]
    with pytest.raises(ValueError, match="unknown Sheets tab"):
        resolve_tabs(["Investors", "Nope"])


async def test_first_sync_creates_every_tab_and_counts_rows(
    ctx: SheetsActivityContext, seed: Seed, sheets: MockSheetsGateway, session_factory: SF
) -> None:
    result = await sync_projection(SyncProjectionInput(idempotency_key="sync:1"))
    assert isinstance(result, SyncProjectionResult)
    assert result.ok and result.duplicate is False and result.failed_tabs == []
    assert result.tabs_created == list(TAB_ORDER)
    assert result.spreadsheet_id == "sheet-1"
    by_tab = {t.tab: t for t in result.tabs}
    assert [t.tab for t in result.tabs] == list(TAB_ORDER)
    assert by_tab["Investors"].created == 2
    assert by_tab["Buy Boxes"].created == 3
    assert by_tab["Outreach Events"].created == 3
    assert by_tab["Contacts"].created == 1
    assert all(t.updated == 0 and t.unchanged == 0 and t.rows == t.created for t in result.tabs)
    assert set(sheets.sheets) == set(TAB_ORDER)
    for tab in TAB_ORDER:
        assert sheets.header(tab) == list(TAB_SPECS[tab].header)
    row = sheets.row(TAB_BUY_BOXES, str(BB_V2_ID))
    assert row is not None and row_as_dict(TAB_BUY_BOXES, row)["investor_id"] == str(INVESTOR_ID)

    events = await _events(session_factory, SYNC_EVENT)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["ok"] is True and payload["total_changed"] == result.total_changed
    assert payload["tabs_created"] == list(TAB_ORDER)
    assert {t["tab"] for t in payload["tabs"]} == set(TAB_ORDER)
    assert payload["mode"] == "mock"


async def test_rerun_without_changes_writes_nothing(
    ctx: SheetsActivityContext, seed: Seed, sheets: MockSheetsGateway
) -> None:
    first = await sync_projection(SyncProjectionInput(idempotency_key="sync:1"))
    second = await sync_projection(SyncProjectionInput(idempotency_key="sync:2"))
    assert second.duplicate is False and second.total_changed == 0
    assert second.tabs_created == []
    for t in second.tabs:
        assert t.created == 0 and t.updated == 0
        assert t.unchanged == next(f.rows for f in first.tabs if f.tab == t.tab)


async def test_retry_with_same_idempotency_key_is_a_no_op(
    ctx: SheetsActivityContext, seed: Seed, sheets: MockSheetsGateway, session_factory: SF
) -> None:
    first = await sync_projection(SyncProjectionInput(idempotency_key="sync:k"))
    calls_after_first = len(sheets.calls)
    again = await sync_projection(SyncProjectionInput(idempotency_key="sync:k"))
    assert again.duplicate is True
    assert [(t.tab, t.created) for t in again.tabs] == [(t.tab, t.created) for t in first.tabs]
    assert len(sheets.calls) == calls_after_first, "no Sheets call on replay"
    assert len(await _events(session_factory, SYNC_EVENT)) == 1


async def test_deactivated_buy_box_changes_status_column_not_row_count(
    ctx: SheetsActivityContext, seed: Seed, sheets: MockSheetsGateway, session_factory: SF
) -> None:
    await sync_projection(SyncProjectionInput(tabs=[TAB_BUY_BOXES], idempotency_key="s1"))
    before = sheets.row(TAB_BUY_BOXES, str(BB_V2_ID))
    assert before is not None and row_as_dict(TAB_BUY_BOXES, before)["status"] == "current"
    async with session_factory() as s:
        box = await s.get(BuyBox, BB_V2_ID)
        assert box is not None
        box.is_active = False
        await s.commit()
    result = await sync_projection(SyncProjectionInput(tabs=[TAB_BUY_BOXES], idempotency_key="s2"))
    [tab] = result.tabs
    assert (tab.created, tab.updated, tab.unchanged) == (0, 1, 2)
    after = sheets.row(TAB_BUY_BOXES, str(BB_V2_ID))
    assert after is not None and row_as_dict(TAB_BUY_BOXES, after)["status"] == STATUS_INACTIVE
    assert len(await sheets.read_keys(TAB_BUY_BOXES)) == 3
    dead = sheets.row(TAB_BUY_BOXES, str(BB_INACTIVE_ID))
    assert dead is not None and row_as_dict(TAB_BUY_BOXES, dead)["status"] == STATUS_INACTIVE


async def test_tab_subset_only_touches_requested_tabs(
    ctx: SheetsActivityContext, seed: Seed, sheets: MockSheetsGateway
) -> None:
    result = await sync_projection(
        SyncProjectionInput(tabs=[TAB_OUTREACH_EVENTS, TAB_INVESTORS], idempotency_key="s")
    )
    assert [t.tab for t in result.tabs] == [TAB_INVESTORS, TAB_OUTREACH_EVENTS]
    assert set(sheets.sheets) == {TAB_INVESTORS, TAB_OUTREACH_EVENTS}


async def test_partial_failure_is_reported_per_tab_and_audited(
    ctx: SheetsActivityContext, seed: Seed, sheets: MockSheetsGateway, session_factory: SF
) -> None:
    await sheets.ensure_tabs([TAB_SPECS[t] for t in TAB_ORDER])
    sheets.fail_next("upsert_rows", RuntimeError("quota"))  # first tab (Investors) fails
    result = await sync_projection(SyncProjectionInput(idempotency_key="s"))
    assert not result.ok and result.failed_tabs == [TAB_INVESTORS]
    inv = result.tabs[0]
    assert inv.error == "RuntimeError: quota" and inv.created == 0 and inv.rows == 2
    assert result.tabs[1].ok and result.tabs[1].created == 1
    [event] = await _events(session_factory, SYNC_EVENT)
    assert event.payload["ok"] is False and event.payload["failed_tabs"] == [TAB_INVESTORS]
    # The next sync picks the failed tab up through the idempotent upsert.
    retry = await sync_projection(SyncProjectionInput(idempotency_key="s2"))
    assert retry.ok and retry.tabs[0].created == 2


async def test_ensure_tabs_failure_fails_every_tab_without_raising(
    ctx: SheetsActivityContext, seed: Seed, sheets: MockSheetsGateway
) -> None:
    sheets.fail_next("ensure_tabs", RuntimeError("403 not shared"))
    result = await sync_projection(SyncProjectionInput(idempotency_key="s"))
    assert result.failed_tabs == list(TAB_ORDER)
    assert all(t.error == "RuntimeError: 403 not shared" for t in result.tabs)
    assert not any(c.method == "upsert_rows" for c in sheets.calls)


async def test_sync_never_writes_to_postgres_tables(
    ctx: SheetsActivityContext, seed: Seed, session_factory: SF
) -> None:
    async with session_factory() as s:
        before_inv = (await s.execute(select(Investor.updated_at))).all()
        before_bb = (await s.execute(select(BuyBox.updated_at))).all()
    audit_before = await _audit_count(session_factory)
    await sync_projection(SyncProjectionInput(idempotency_key="s"))
    async with session_factory() as s:
        assert (await s.execute(select(Investor.updated_at))).all() == before_inv
        assert (await s.execute(select(BuyBox.updated_at))).all() == before_bb
    assert await _audit_count(session_factory) == audit_before + 1  # only the sheets.sync event


async def test_sync_with_empty_database_creates_tabs_only(
    ctx: SheetsActivityContext, sheets: MockSheetsGateway
) -> None:
    result = await sync_projection(SyncProjectionInput(idempotency_key="s"))
    assert result.ok and result.total_changed == 0 and result.tabs_created == list(TAB_ORDER)
    assert all(t.rows == 0 for t in result.tabs)


# --------------------------------------------------------------------------------------------
# reconcile_sources
# --------------------------------------------------------------------------------------------


def _inject_known_inbound(graph: MockGraphGateway) -> None:
    graph.inject_inbound(
        from_address="alex@acme.test",
        subject="RE: Houston deals",
        body="SFR ...",
        internet_message_id=INBOUND_EXTERNAL_ID,
        received_at=NOW - timedelta(hours=2),
    )


async def test_clean_state_reports_no_mismatches_and_no_alert(
    ctx: SheetsActivityContext,
    seed: Seed,
    sheets: MockSheetsGateway,
    graph: MockGraphGateway,
    session_factory: SF,
) -> None:
    _inject_known_inbound(graph)
    # Seeded inbound was received at T1 (the 24th); it is outside a 24 h window from NOW unless
    # we widen the window, so use 48 h to include it on both sides.
    await sync_projection(SyncProjectionInput(idempotency_key="s"))
    result = await reconcile_sources(ReconcileInput(lookback_hours=48, idempotency_key="r1"))
    assert result.ok and result.mismatches == [] and result.alerted is False
    assert result.outlook_messages == 1 and result.postgres_inbound_messages == 1
    assert result.tabs_checked == list(TAB_ORDER)
    assert result.duplicate is False
    [event] = await _events(session_factory, RECONCILIATION_EVENT)
    assert event.payload["mismatch_count"] == 0 and event.payload["alerted"] is False
    assert await _events(session_factory, "alert.raised") == []


async def test_detects_outlook_message_absent_from_postgres_and_writes_nothing(
    ctx: SheetsActivityContext,
    seed: Seed,
    graph: MockGraphGateway,
    session_factory: SF,
) -> None:
    await sync_projection(SyncProjectionInput(idempotency_key="s"))
    _inject_known_inbound(graph)
    stray = graph.inject_inbound(
        from_address="new@investor.test",
        subject="hello",
        body="never persisted",
        received_at=NOW - timedelta(hours=1),
    )
    async with session_factory() as s:
        messages_before = int((await s.execute(select(func.count(Message.id)))).scalar_one())
    result = await reconcile_sources(ReconcileInput(lookback_hours=48, idempotency_key="r"))
    assert result.count(MISMATCH_OUTLOOK_MISSING_IN_POSTGRES) == 1
    [m] = [m for m in result.mismatches if m.kind == MISMATCH_OUTLOOK_MISSING_IN_POSTGRES]
    assert m.key == stray.external_message_id and m.source == "outlook"
    assert result.alerted is True
    async with session_factory() as s:
        assert (
            int((await s.execute(select(func.count(Message.id)))).scalar_one()) == messages_before
        )
        inbound = (
            (await s.execute(select(Message).where(Message.direction == Direction.IN)))
            .scalars()
            .all()
        )
        assert [x.external_message_id for x in inbound] == [INBOUND_EXTERNAL_ID]
    [alert] = await _events(session_factory, "alert.raised")
    assert alert.payload["kind"] == ALERT_KIND_RECONCILIATION
    assert alert.payload["summary"] == {MISMATCH_OUTLOOK_MISSING_IN_POSTGRES: 1}
    assert "nothing was changed" in alert.payload["detail"]


async def test_detects_postgres_inbound_missing_from_outlook(
    ctx: SheetsActivityContext, seed: Seed, graph: MockGraphGateway
) -> None:
    await sync_projection(SyncProjectionInput(idempotency_key="s"))
    # Mailbox is empty: the seeded inbound row has no mailbox counterpart.
    result = await reconcile_sources(ReconcileInput(lookback_hours=48, idempotency_key="r"))
    assert result.count(MISMATCH_POSTGRES_MISSING_IN_OUTLOOK) == 1
    assert result.mismatches[0].key == INBOUND_EXTERNAL_ID
    assert result.outlook_messages == 0 and result.postgres_inbound_messages == 1


async def test_detects_missing_sheets_key_orphan_and_stale_row(
    ctx: SheetsActivityContext,
    seed: Seed,
    sheets: MockSheetsGateway,
    graph: MockGraphGateway,
    session_factory: SF,
) -> None:
    _inject_known_inbound(graph)
    await sync_projection(SyncProjectionInput(idempotency_key="s"))
    # 1. A buy box added after the last sync -> missing key.
    async with session_factory() as s:
        s.add(
            BuyBox(
                investor_id=INVESTOR_ID,
                buy_box_key="land",
                version=1,
                geography={},
                asset_types=["land"],
            )
        )
        await s.commit()
    # 2. A row somebody typed into the sheet -> orphan key.
    sheets.append_raw(TAB_INVESTORS, ["not-a-real-id", "active", "Typed By Hand"])
    # 3. An operator edit of a projected cell -> stale row.
    sheets.set_cell(TAB_BUY_BOXES, str(BB_V2_ID), "price_max", 999)
    sheet_rows_before = {t: await sheets.read_rows(t) for t in TAB_ORDER}

    result = await reconcile_sources(ReconcileInput(lookback_hours=48, idempotency_key="r"))
    kinds = {(m.kind, m.tab) for m in result.mismatches}
    assert (MISMATCH_SHEETS_MISSING_KEY, TAB_BUY_BOXES) in kinds
    assert (MISMATCH_SHEETS_ORPHAN_KEY, TAB_INVESTORS) in kinds
    assert (MISMATCH_SHEETS_STALE_ROW, TAB_BUY_BOXES) in kinds
    stale = next(m for m in result.mismatches if m.kind == MISMATCH_SHEETS_STALE_ROW)
    assert stale.key == str(BB_V2_ID) and stale.detail == "columns differ: price_max"
    orphan = next(m for m in result.mismatches if m.kind == MISMATCH_SHEETS_ORPHAN_KEY)
    assert orphan.key == "not-a-real-id"
    assert len(result.mismatches) == 3 and result.alerted
    # Nothing was repaired on either side.
    assert {t: await sheets.read_rows(t) for t in TAB_ORDER} == sheet_rows_before
    assert not any(c.method == "upsert_rows" for c in sheets.calls[-len(TAB_ORDER) :])
    [event] = await _events(session_factory, RECONCILIATION_EVENT)
    assert event.payload["summary"] == {
        MISMATCH_SHEETS_MISSING_KEY: 1,
        MISMATCH_SHEETS_ORPHAN_KEY: 1,
        MISMATCH_SHEETS_STALE_ROW: 1,
    }
    assert event.payload["truncated"] is False


async def test_missing_tab_and_unavailable_graph_are_reported_not_raised(
    session_factory: SF, sheets: MockSheetsGateway, seed: Seed
) -> None:
    settings = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    set_sheets_context(
        SheetsActivityContext(
            sheets=sheets,
            session_factory=session_factory,
            settings=settings,
            graph=None,
            clock=lambda: NOW,
        )
    )
    try:
        result = await reconcile_sources(ReconcileInput(tabs=[TAB_INVESTORS], idempotency_key="r"))
    finally:
        set_sheets_context(None)
    assert result.count(MISMATCH_SOURCE_UNAVAILABLE) == 1
    assert result.count(MISMATCH_SHEETS_TAB_MISSING) == 1
    assert result.tabs_checked == [TAB_INVESTORS]


async def test_graph_error_is_reported_as_source_unavailable(
    ctx: SheetsActivityContext, seed: Seed, graph: MockGraphGateway
) -> None:
    await sync_projection(SyncProjectionInput(idempotency_key="s"))
    graph.fail_next("delta_messages", RuntimeError("graph down"))
    result = await reconcile_sources(ReconcileInput(lookback_hours=48, idempotency_key="r"))
    [m] = [m for m in result.mismatches if m.kind == MISMATCH_SOURCE_UNAVAILABLE]
    assert "graph down" in m.detail and m.source == "outlook"


async def test_reconcile_retry_with_same_key_returns_recorded_result(
    ctx: SheetsActivityContext, seed: Seed, graph: MockGraphGateway, session_factory: SF
) -> None:
    first = await reconcile_sources(ReconcileInput(lookback_hours=48, idempotency_key="r"))
    again = await reconcile_sources(ReconcileInput(lookback_hours=48, idempotency_key="r"))
    assert again.duplicate is True and again.alerted == first.alerted
    assert [m.kind for m in again.mismatches] == [m.kind for m in first.mismatches]
    assert len(await _events(session_factory, RECONCILIATION_EVENT)) == 1
    assert len(await _events(session_factory, "alert.raised")) == 1
