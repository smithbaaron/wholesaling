"""A6 Parser Formatter (REQUIREMENTS.md INV-20, INT-21).

Converts one confirmed buy-box version into a labelled parser email for the optional Zapier
Email Parser path (behind ``ZAPIER_PARSER_ENABLED``, default false, RUL-07). The format is a
contract with the Zap, so it is deterministic and versioned:

- one ``LABEL: value`` line per field, labels upper snake case, stable order;
- ``---`` separates the structured block from the free-text block;
- ``RAW_TEXT`` is last because the parser reads it to the terminator; every other value is
  flattened to one line;
- ``BEGIN_BUY_BOX`` / ``END_BUY_BOX`` bracket the message so a parser template can anchor.

A6 is a pure formatter: no model call, no DB access, no sending. Sending is
``activities/zapier.py``, which goes through the Graph draft/send path like every other outbound
message (INT-13, RUL-06).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from wholesaling.db.models.buy_boxes import BuyBox
from wholesaling.db.models.enums import AgentId
from wholesaling.db.models.investors import Investor
from wholesaling.integrations.sheets.interface import iso_utc
from wholesaling.integrations.sheets.projection import buy_box_status

PARSER_FORMAT_VERSION = "1"
SUBJECT_PREFIX = "[BUYBOX]"
BEGIN_MARKER = "BEGIN_BUY_BOX"
END_MARKER = "END_BUY_BOX"
SEPARATOR = "---"

# Labels in message order. Changing this tuple is a format change: bump PARSER_FORMAT_VERSION
# and update the Zapier template (docs/integrations/zapier.md).
STRUCTURED_LABELS: tuple[str, ...] = (
    "FORMAT_VERSION",
    "ROW_KEY",
    "INVESTOR_ID",
    "INVESTOR_NAME",
    "BUY_BOX_ID",
    "BUY_BOX_KEY",
    "VERSION",
    "STATUS",
    "SOURCE_MESSAGE_ID",
    "SOURCE_REF",
    "MARKETS",
    "CITIES",
    "ZIPS",
    "NEIGHBORHOODS",
    "ASSET_TYPES",
    "PRICE_MIN",
    "PRICE_MAX",
    "CONDITION",
    "STRATEGY",
    "FINANCING",
    "CLOSING_PREFERENCES",
    "OTHER_CRITERIA",
    "UNKNOWN_FIELDS",
    "CHANGE_NOTE",
    "UPDATED_AT",
)
RAW_TEXT_LABEL = "RAW_TEXT"


@dataclass(frozen=True, slots=True)
class ParserEmail:
    subject: str
    body: str
    buy_box_id: str
    version: int

    @property
    def idempotency_key(self) -> str:
        """One parser email per (buy box, version): the Graph draft idempotency key."""
        return parser_idempotency_key(self.buy_box_id, self.version)


def parser_idempotency_key(buy_box_id: str, version: int) -> str:
    return f"zapier-parser:{buy_box_id}:v{version}"


def parser_row_key(investor_id: uuid.UUID | str, buy_box_key: str) -> str:
    """The Zap's lookup key: one logical buy box (INT-22 "one buy box") across its versions.
    ``buy_box_id`` alone would be one row per *version* because ``buy_boxes`` is versioned."""
    return f"{investor_id}|{buy_box_key}"


def _one_line(value: Any) -> str:
    """Flatten any value to a single line with no stray whitespace; ``None`` is empty."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(int(value)) if value == value.to_integral_value() else format(value, "f")
    text = str(getattr(value, "value", value))
    return " ".join(text.split())


def _joined(values: Iterable[Any] | None) -> str:
    return "; ".join(_one_line(v) for v in values) if values else ""


def _json(value: Mapping[str, Any] | Sequence[Any] | None) -> str:
    if not value:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class A6ParserFormatter:
    """Format a buy box version as a parser email. Stateless; one instance serves all calls."""

    agent_id = AgentId.A6
    FORMAT_VERSION = PARSER_FORMAT_VERSION

    def fields(self, buy_box: BuyBox, investor: Investor) -> dict[str, str]:
        """The structured ``{LABEL: value}`` map in message order (what the Zap sees)."""
        geo = buy_box.geography or {}
        values: dict[str, str] = {
            "FORMAT_VERSION": self.FORMAT_VERSION,
            "ROW_KEY": parser_row_key(investor.id, buy_box.buy_box_key),
            "INVESTOR_ID": str(investor.id),
            "INVESTOR_NAME": _one_line(investor.canonical_name),
            "BUY_BOX_ID": str(buy_box.id),
            "BUY_BOX_KEY": _one_line(buy_box.buy_box_key),
            "VERSION": str(buy_box.version),
            "STATUS": buy_box_status(buy_box),
            "SOURCE_MESSAGE_ID": _one_line(buy_box.source_message_id),
            "SOURCE_REF": _one_line(buy_box.source_ref),
            "MARKETS": _joined(geo.get("markets")),
            "CITIES": _joined(geo.get("cities")),
            "ZIPS": _joined(geo.get("zips")),
            "NEIGHBORHOODS": _joined(geo.get("neighborhoods")),
            "ASSET_TYPES": _joined(buy_box.asset_types),
            "PRICE_MIN": _one_line(buy_box.price_min),
            "PRICE_MAX": _one_line(buy_box.price_max),
            "CONDITION": _one_line(buy_box.condition),
            "STRATEGY": _one_line(buy_box.strategy),
            "FINANCING": _one_line(buy_box.financing),
            "CLOSING_PREFERENCES": _json(buy_box.closing_preferences),
            "OTHER_CRITERIA": _json(buy_box.other_criteria),
            "UNKNOWN_FIELDS": _joined(buy_box.unknown_fields),
            "CHANGE_NOTE": _one_line(buy_box.change_note),
            "UPDATED_AT": iso_utc(buy_box.updated_at) or "",
        }
        assert tuple(values) == STRUCTURED_LABELS
        return values

    def format(self, buy_box: BuyBox, investor: Investor) -> ParserEmail:
        values = self.fields(buy_box, investor)
        lines = [BEGIN_MARKER]
        # ``LABEL: value``; an empty value leaves ``LABEL:`` with no trailing space.
        lines.extend(f"{label}: {values[label]}".rstrip() for label in STRUCTURED_LABELS)
        lines.append(SEPARATOR)
        lines.append(f"{RAW_TEXT_LABEL}:")
        lines.append((buy_box.raw_text or "").strip())
        lines.append(END_MARKER)
        subject = f"{SUBJECT_PREFIX} {investor.id} {buy_box.buy_box_key} v{buy_box.version}"
        return ParserEmail(
            subject=subject,
            body="\n".join(lines) + "\n",
            buy_box_id=str(buy_box.id),
            version=buy_box.version,
        )


__all__ = [
    "BEGIN_MARKER",
    "END_MARKER",
    "PARSER_FORMAT_VERSION",
    "RAW_TEXT_LABEL",
    "SEPARATOR",
    "STRUCTURED_LABELS",
    "SUBJECT_PREFIX",
    "A6ParserFormatter",
    "ParserEmail",
    "parser_idempotency_key",
    "parser_row_key",
]
