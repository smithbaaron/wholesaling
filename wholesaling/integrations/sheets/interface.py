"""What the application needs from Google Sheets, kept deliberately narrow.

Sheets is a one-way operator projection of Postgres (REQUIREMENTS.md STK-02, STK-06, INT-14,
WF-13). Nothing here reads a value back into the system of record and there is **no delete
operation on this surface**: a row for an entity that was deactivated or superseded keeps its
place and gets a new ``status`` value. ``upsert_rows`` is idempotent by key column, so a sync
that runs twice changes nothing the second time.

Everything outside ``integrations.sheets`` talks to ``SheetsGateway`` so it runs unchanged
against the in-memory mock (``SHEETS_MODE=mock``, the default) and the live client.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from wholesaling.integrations.base import HealthStatus, IntegrationMode

# One cell. ``None`` is written as an empty cell.
Cell = str | int | float | bool | None
SheetRow = list[Cell]

# Header is row 1; the first data row is row 2 (Sheets rows are 1-based).
HEADER_ROW_NUMBER = 1
FIRST_DATA_ROW_NUMBER = 2


class SheetsError(Exception):
    """Base class for every error the gateway raises."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class SheetsAuthError(SheetsError):
    """Token acquisition failed, or Sheets rejected the token / the sheet is not shared with the
    service account (401/403)."""


class SheetsNotFound(SheetsError):
    """The spreadsheet or a tab does not exist (404)."""


class SheetsThrottled(SheetsError):
    """Sheets returned 429 / 5xx after the retry budget was spent."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class SheetsCircuitOpen(SheetsError):
    """The circuit breaker is open; no request was attempted (XC-08)."""


@dataclass(frozen=True, slots=True)
class TabSpec:
    """The contract for one tab: its name, its header row and which column is the key.

    The key column is always the first one (a stable Postgres id, INT-14), so ``key_col`` is 0
    for every tab the projection ships; it is a field so the gateway never hard-codes it.
    """

    name: str
    header: tuple[str, ...]
    key_col: int = 0
    requirement: str = ""

    @property
    def key_name(self) -> str:
        return self.header[self.key_col]

    @property
    def width(self) -> int:
        return len(self.header)

    def __post_init__(self) -> None:
        if not self.header:
            msg = f"tab {self.name!r} needs at least one header column"
            raise ValueError(msg)
        if not 0 <= self.key_col < len(self.header):
            msg = f"tab {self.name!r}: key_col {self.key_col} outside header"
            raise ValueError(msg)
        if "status" not in self.header:
            msg = f"tab {self.name!r} needs a 'status' column (one-way projection never deletes)"
            raise ValueError(msg)


@dataclass(slots=True)
class UpsertResult:
    """Counts for one ``upsert_rows`` call."""

    tab: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    header_written: bool = False

    @property
    def changed(self) -> int:
        return self.created + self.updated

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged


@dataclass(slots=True)
class RecordedCall:
    """One recorded method call on the mock."""

    method: str
    args: dict[str, Any] = field(default_factory=dict)


def canon_cell(value: Any) -> str:
    """Canonical string of one cell for change detection.

    Sheets returns numbers as floats and booleans as bools; Postgres gives ``Decimal``, ``int``
    and ``datetime``. Two cells are "the same" when their canonical forms match, so a value that
    round-trips through the API without changing never counts as an update.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float | Decimal):
        try:
            dec = Decimal(str(value))
        except InvalidOperation:  # pragma: no cover - nan / inf never leave the projection
            return str(value)
        if dec == dec.to_integral_value():
            return str(int(dec))
        return format(dec.normalize(), "f")
    if isinstance(value, datetime):
        return iso_utc(value) or ""
    text = str(value)
    if not text or text.strip() != text or text in ("TRUE", "FALSE"):
        return text
    # A numeric string the API may hand back as a number (we write RAW, but be tolerant).
    try:
        dec = Decimal(text)
    except InvalidOperation:
        return text
    if not dec.is_finite():
        return text
    if dec == dec.to_integral_value():
        return str(int(dec))
    return format(dec.normalize(), "f")


def canon_row(row: Sequence[Any], width: int) -> tuple[str, ...]:
    """Canonical tuple padded/truncated to ``width`` so a short row read back from the API
    (trailing empties are omitted) compares equal to the row we wrote."""
    cells = [canon_cell(v) for v in row[:width]]
    cells.extend("" for _ in range(width - len(cells)))
    return tuple(cells)


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 in UTC with a ``Z`` suffix; naive datetimes (SQLite) are treated as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@runtime_checkable
class SheetsGateway(Protocol):
    name: str
    mode: IntegrationMode

    async def healthcheck(self) -> HealthStatus: ...

    async def ensure_tabs(self, specs: Sequence[TabSpec]) -> list[str]:
        """Create every missing tab with its header row. Returns the names created. Existing
        tabs are left alone (their header is checked and rewritten by ``upsert_rows``)."""
        ...

    async def read_keys(self, tab: str, key_col: int = 0) -> dict[str, int]:
        """``{key: row_number}`` for every data row (row 1 is the header)."""
        ...

    async def read_rows(self, tab: str) -> list[SheetRow]:
        """Every data row (header excluded), in sheet order, trailing empties trimmed."""
        ...

    async def upsert_rows(
        self, spec: TabSpec, rows: Sequence[Sequence[Cell]], *, key_col: int | None = None
    ) -> UpsertResult:
        """Write ``rows`` into ``spec.name`` keyed by ``key_col`` (default ``spec.key_col``).

        Existing keys are rewritten in place only when their cells differ; new keys are appended
        below the last row. Nothing is ever deleted. Batched so a tab of thousands of rows is a
        handful of API calls.
        """
        ...


__all__ = [
    "FIRST_DATA_ROW_NUMBER",
    "HEADER_ROW_NUMBER",
    "Cell",
    "RecordedCall",
    "SheetRow",
    "SheetsAuthError",
    "SheetsCircuitOpen",
    "SheetsError",
    "SheetsGateway",
    "SheetsNotFound",
    "SheetsThrottled",
    "TabSpec",
    "UpsertResult",
    "canon_cell",
    "canon_row",
    "iso_utc",
]
