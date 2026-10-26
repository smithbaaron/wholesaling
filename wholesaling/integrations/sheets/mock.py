"""In-memory Sheets gateway. Selected when ``SHEETS_MODE=mock`` (the default).

Same semantics as the live client so the sync activity, the reconciliation activity and the
synthetic pilot (BO-09) behave identically against both:

- tabs are ``{name: [header, row, row, ...]}``; row numbers are 1-based like Sheets;
- ``upsert_rows`` rewrites a row only when its canonical cells differ, appends unknown keys,
  never deletes;
- every call is recorded in ``calls``; ``fail_next(method, exc)`` injects one failure;
- ``read_rows`` trims trailing empty cells the way the API omits them.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.sheets.interface import (
    FIRST_DATA_ROW_NUMBER,
    Cell,
    RecordedCall,
    SheetRow,
    SheetsNotFound,
    TabSpec,
    UpsertResult,
    canon_row,
)


def _trim(row: Sequence[Cell]) -> SheetRow:
    cells = list(row)
    while cells and (cells[-1] is None or cells[-1] == ""):
        cells.pop()
    return cells


class MockSheetsGateway:
    name = "sheets"
    mode: IntegrationMode = "mock"

    def __init__(self, spreadsheet_id: str = "mock-spreadsheet") -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheets: dict[str, list[SheetRow]] = {}
        self.calls: list[RecordedCall] = []
        self.healthy = True
        self._failures: dict[str, deque[Exception]] = {}

    # -- test helpers ------------------------------------------------------------------------

    def fail_next(self, method: str, exc: Exception) -> None:
        self._failures.setdefault(method, deque()).append(exc)

    def reset(self) -> None:
        self.sheets.clear()
        self.calls.clear()
        self._failures.clear()
        self.healthy = True

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append(RecordedCall(method=method, args=args))
        pending = self._failures.get(method)
        if pending:
            raise pending.popleft()

    def header(self, tab: str) -> SheetRow:
        return list(self._tab(tab)[0]) if self._tab(tab) else []

    def row(self, tab: str, key: str, key_col: int = 0) -> SheetRow | None:
        """The data row holding ``key`` (tests and the pilot report assert on these)."""
        for r in self._tab(tab)[1:]:
            if key_col < len(r) and str(r[key_col]) == key:
                return list(r)
        return None

    def set_cell(self, tab: str, key: str, column: str, value: Cell) -> None:
        """Simulate an operator editing a projected cell (reconciliation must flag it)."""
        rows = self._tab(tab)
        col = list(rows[0]).index(column)
        for r in rows[1:]:
            if str(r[0]) == key:
                while len(r) <= col:
                    r.append(None)
                r[col] = value
                return
        raise SheetsNotFound(f"key {key!r} not in tab {tab!r}")

    def append_raw(self, tab: str, row: Sequence[Cell]) -> None:
        """Simulate a row that exists only in the sheet (reconciliation must flag it)."""
        self._tab(tab).append(list(row))

    def _tab(self, tab: str) -> list[SheetRow]:
        rows = self.sheets.get(tab)
        if rows is None:
            raise SheetsNotFound(f"tab {tab!r} not found", status_code=400, code="INVALID_ARGUMENT")
        return rows

    # -- SheetsGateway -----------------------------------------------------------------------

    async def healthcheck(self) -> HealthStatus:
        self._record("healthcheck")
        return HealthStatus(
            name=self.name,
            mode=self.mode,
            ok=self.healthy,
            detail="mock gateway" if self.healthy else "mock gateway forced unhealthy",
        )

    async def ensure_tabs(self, specs: Sequence[TabSpec]) -> list[str]:
        self._record("ensure_tabs", tabs=[s.name for s in specs])
        created: list[str] = []
        for spec in specs:
            if spec.name not in self.sheets:
                self.sheets[spec.name] = [list(spec.header)]
                created.append(spec.name)
        return created

    async def read_keys(self, tab: str, key_col: int = 0) -> dict[str, int]:
        self._record("read_keys", tab=tab, key_col=key_col)
        keys: dict[str, int] = {}
        for offset, r in enumerate(self._tab(tab)[1:]):
            if key_col < len(r) and r[key_col] not in (None, ""):
                keys.setdefault(str(r[key_col]), FIRST_DATA_ROW_NUMBER + offset)
        return keys

    async def read_rows(self, tab: str) -> list[SheetRow]:
        self._record("read_rows", tab=tab)
        return [_trim(r) for r in self._tab(tab)[1:]]

    async def upsert_rows(
        self, spec: TabSpec, rows: Sequence[Sequence[Cell]], *, key_col: int | None = None
    ) -> UpsertResult:
        kc = spec.key_col if key_col is None else key_col
        self._record("upsert_rows", tab=spec.name, rows=len(rows), key_col=kc)
        if spec.name not in self.sheets:
            self.sheets[spec.name] = [list(spec.header)]
        sheet = self.sheets[spec.name]
        result = UpsertResult(tab=spec.name)
        if canon_row(sheet[0], spec.width) != canon_row(spec.header, spec.width):
            sheet[0] = list(spec.header)
            result.header_written = True
        index = {str(r[kc]): i for i, r in enumerate(sheet) if i > 0 and kc < len(r)}
        for incoming in rows:
            row = list(incoming)
            key = str(row[kc])
            at = index.get(key)
            if at is None:
                sheet.append(row)
                index[key] = len(sheet) - 1
                result.created += 1
            elif canon_row(sheet[at], spec.width) != canon_row(row, spec.width):
                sheet[at] = row
                result.updated += 1
            else:
                result.unchanged += 1
        return result


__all__ = ["MockSheetsGateway"]
