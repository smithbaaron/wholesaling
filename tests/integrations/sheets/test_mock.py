"""MockSheetsGateway: upsert idempotency, status-instead-of-delete, header repair, recording."""

from __future__ import annotations

import pytest

from wholesaling.config.settings import Settings
from wholesaling.integrations.sheets import (
    MockSheetsGateway,
    SheetsGateway,
    SheetsNotFound,
    TabSpec,
    get_sheets,
)

SPEC = TabSpec(name="Buy Boxes", header=("buy_box_id", "investor_id", "status", "price_max"))


@pytest.fixture
def gw() -> MockSheetsGateway:
    return MockSheetsGateway()


def test_factory_returns_mock_by_default() -> None:
    s = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    gw = get_sheets(s)
    assert isinstance(gw, MockSheetsGateway)
    assert isinstance(gw, SheetsGateway)
    assert gw.mode == "mock" and gw.name == "sheets"


async def test_healthcheck_reports_mode_and_can_be_forced_unhealthy(gw: MockSheetsGateway) -> None:
    ok = await gw.healthcheck()
    assert ok.ok and ok.mode == "mock" and ok.name == "sheets"
    gw.healthy = False
    bad = await gw.healthcheck()
    assert not bad.ok


async def test_ensure_tabs_creates_header_once(gw: MockSheetsGateway) -> None:
    assert await gw.ensure_tabs([SPEC]) == ["Buy Boxes"]
    assert await gw.ensure_tabs([SPEC]) == []
    assert gw.header("Buy Boxes") == list(SPEC.header)
    assert await gw.read_rows("Buy Boxes") == []
    assert await gw.read_keys("Buy Boxes") == {}


async def test_upsert_is_idempotent_by_key(gw: MockSheetsGateway) -> None:
    await gw.ensure_tabs([SPEC])
    rows = [["bb-1", "inv-1", "current", 250000], ["bb-2", "inv-1", "current", 100000]]
    first = await gw.upsert_rows(SPEC, rows)
    assert (first.created, first.updated, first.unchanged) == (2, 0, 0)
    second = await gw.upsert_rows(SPEC, rows)
    assert (second.created, second.updated, second.unchanged) == (0, 0, 2)
    assert second.changed == 0 and second.total == 2
    assert await gw.read_keys("Buy Boxes") == {"bb-1": 2, "bb-2": 3}


async def test_upsert_rewrites_changed_rows_in_place_and_appends_new(gw: MockSheetsGateway) -> None:
    await gw.ensure_tabs([SPEC])
    await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 250000]])
    result = await gw.upsert_rows(
        SPEC, [["bb-1", "inv-1", "inactive", 250000], ["bb-3", "inv-2", "current", 1]]
    )
    assert (result.created, result.updated, result.unchanged) == (1, 1, 0)
    assert await gw.read_keys("Buy Boxes") == {"bb-1": 2, "bb-3": 3}
    assert gw.row("Buy Boxes", "bb-1") == ["bb-1", "inv-1", "inactive", 250000]


async def test_numeric_round_trip_does_not_count_as_update(gw: MockSheetsGateway) -> None:
    await gw.ensure_tabs([SPEC])
    await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 250000]])
    again = await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 250000.0]])
    assert again.unchanged == 1 and again.updated == 0
    none_vs_empty = await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 250000]])
    assert none_vs_empty.unchanged == 1


async def test_no_delete_surface_rows_are_never_removed(gw: MockSheetsGateway) -> None:
    await gw.ensure_tabs([SPEC])
    await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1], ["bb-2", "inv-1", "current", 2]])
    # A later sync that no longer carries bb-2 leaves its row in place.
    await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1]])
    assert set(await gw.read_keys("Buy Boxes")) == {"bb-1", "bb-2"}
    assert not any(hasattr(gw, name) for name in ("delete_rows", "delete_keys", "clear"))


async def test_header_mismatch_is_repaired_on_upsert(gw: MockSheetsGateway) -> None:
    gw.sheets["Buy Boxes"] = [["buy_box_id", "old_col"]]
    result = await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1]])
    assert result.header_written is True
    assert gw.header("Buy Boxes") == list(SPEC.header)


async def test_upsert_creates_missing_tab(gw: MockSheetsGateway) -> None:
    result = await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1]])
    assert result.created == 1
    assert gw.header("Buy Boxes") == list(SPEC.header)


async def test_unknown_tab_raises_not_found(gw: MockSheetsGateway) -> None:
    with pytest.raises(SheetsNotFound):
        await gw.read_keys("Missing")
    with pytest.raises(SheetsNotFound):
        await gw.read_rows("Missing")


async def test_calls_are_recorded_and_failures_injectable(gw: MockSheetsGateway) -> None:
    await gw.ensure_tabs([SPEC])
    gw.fail_next("upsert_rows", RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1]])
    ok = await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", 1]])
    assert ok.created == 1
    assert [c.method for c in gw.calls] == ["ensure_tabs", "upsert_rows", "upsert_rows"]
    assert gw.calls[0].args == {"tabs": ["Buy Boxes"]}
    gw.reset()
    assert gw.calls == [] and gw.sheets == {}


async def test_read_rows_trims_trailing_empties_and_set_cell_edits(gw: MockSheetsGateway) -> None:
    await gw.ensure_tabs([SPEC])
    await gw.upsert_rows(SPEC, [["bb-1", "inv-1", "current", None]])
    assert await gw.read_rows("Buy Boxes") == [["bb-1", "inv-1", "current"]]
    gw.set_cell("Buy Boxes", "bb-1", "price_max", 5)
    assert await gw.read_rows("Buy Boxes") == [["bb-1", "inv-1", "current", 5]]
    gw.append_raw("Buy Boxes", ["ghost", "x", "current", 0])
    assert "ghost" in await gw.read_keys("Buy Boxes")
    with pytest.raises(SheetsNotFound):
        gw.set_cell("Buy Boxes", "nope", "price_max", 1)
