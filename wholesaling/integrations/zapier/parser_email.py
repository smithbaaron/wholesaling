"""The Zapier side of the parser contract (REQUIREMENTS.md INT-21, INT-22).

A6 (``agents/a6.py``) writes the email; this module describes what the Zapier Email Parser
template must extract from it and offers ``parse_parser_email`` as the reference parser so the
acceptance tests (INT-22) can prove two emails for one buy box resolve to the same row key and a
replayed email is a no-op. Production code never parses its own emails; the Zap does.
"""

from __future__ import annotations

from dataclasses import dataclass

from wholesaling.agents.a6 import (
    BEGIN_MARKER,
    END_MARKER,
    PARSER_FORMAT_VERSION,
    RAW_TEXT_LABEL,
    SEPARATOR,
    STRUCTURED_LABELS,
)

# Zapier Email Parser templates mark extracted fields as {{name}}; names are lower case here
# so they read naturally in the Zap editor ("row_key").
# The Zap looks the Sheets row up by ROW_KEY (``investor_id|buy_box_key``): one row per logical
# buy box across versions (INT-22). A replayed message repeats the same (ROW_KEY, VERSION).
ROW_KEY_LABEL = "ROW_KEY"
REPLAY_KEY_LABELS: tuple[str, ...] = ("ROW_KEY", "VERSION")


def parser_template() -> str:
    """The exact text to paste into the Zapier Email Parser template editor."""
    lines = [BEGIN_MARKER]
    lines.extend(f"{label}: {{{{{label.lower()}}}}}" for label in STRUCTURED_LABELS)
    lines.append(SEPARATOR)
    lines.append(f"{RAW_TEXT_LABEL}:")
    lines.append(f"{{{{{RAW_TEXT_LABEL.lower()}}}}}")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class ParsedParserEmail:
    fields: dict[str, str]

    @property
    def row_key(self) -> str:
        """The Sheets lookup key the Zap uses (``investor_id|buy_box_key``)."""
        return self.fields.get(ROW_KEY_LABEL, "")

    @property
    def replay_key(self) -> tuple[str, ...]:
        """``(row_key, version)``: equal for a replayed message, different for a new version."""
        return tuple(self.fields.get(label, "") for label in REPLAY_KEY_LABELS)

    @property
    def format_version(self) -> str:
        return self.fields.get("FORMAT_VERSION", "")

    @property
    def is_supported(self) -> bool:
        return self.format_version == PARSER_FORMAT_VERSION


def parse_parser_email(body: str) -> ParsedParserEmail:
    """Reference parser mirroring what the Zapier template extracts."""
    inside = False
    values: dict[str, str] = {}
    raw: list[str] | None = None
    for line in body.splitlines():
        if line == BEGIN_MARKER:
            inside = True
            continue
        if not inside:
            continue
        if line == END_MARKER:
            break
        if raw is not None:
            raw.append(line)
            continue
        if line == SEPARATOR:
            continue
        if line == f"{RAW_TEXT_LABEL}:":
            raw = []
            continue
        label, sep, value = line.partition(":")
        if sep and label in STRUCTURED_LABELS:
            values[label] = value.strip()
    values[RAW_TEXT_LABEL] = "\n".join(raw or []).strip()
    return ParsedParserEmail(fields=values)


__all__ = [
    "REPLAY_KEY_LABELS",
    "ROW_KEY_LABEL",
    "ParsedParserEmail",
    "parse_parser_email",
    "parser_template",
]
