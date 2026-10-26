"""Google Sheets one-way projection: Protocol, tab contract, mock, live client, factory.

Selected by ``SHEETS_MODE`` (``mock`` by default). Postgres is the only writer of truth; this
package only ever writes *to* Sheets and never reads a value back into the system of record
(REQUIREMENTS.md STK-02, GEN-05 rejected). See docs/integrations/sheets.md.
"""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.sheets.interface import (
    FIRST_DATA_ROW_NUMBER,
    HEADER_ROW_NUMBER,
    Cell,
    RecordedCall,
    SheetRow,
    SheetsAuthError,
    SheetsCircuitOpen,
    SheetsError,
    SheetsGateway,
    SheetsNotFound,
    SheetsThrottled,
    TabSpec,
    UpsertResult,
    canon_cell,
    canon_row,
    iso_utc,
)
from wholesaling.integrations.sheets.mock import MockSheetsGateway
from wholesaling.integrations.sheets.projection import (
    TAB_ORDER,
    TAB_SPECS,
    ProjectionSnapshot,
    build_rows,
    row_as_dict,
)


def get_sheets(settings: Settings) -> SheetsGateway:
    """Return the gateway implementation selected by ``SHEETS_MODE``."""
    if settings.sheets_mode == "mock":
        return MockSheetsGateway(
            spreadsheet_id=settings.sheets_spreadsheet_id or "mock-spreadsheet"
        )
    # Imported lazily so mock-mode processes never import the live module (RUL-02 pattern).
    from wholesaling.integrations.sheets.live import LiveSheetsGateway

    return LiveSheetsGateway(settings)


__all__ = [
    "FIRST_DATA_ROW_NUMBER",
    "HEADER_ROW_NUMBER",
    "TAB_ORDER",
    "TAB_SPECS",
    "Cell",
    "MockSheetsGateway",
    "ProjectionSnapshot",
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
    "build_rows",
    "canon_cell",
    "canon_row",
    "get_sheets",
    "iso_utc",
    "row_as_dict",
]
