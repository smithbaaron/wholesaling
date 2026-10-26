"""Optional Zapier Email Parser path (REQUIREMENTS.md INT-20..22, RUL-07).

Off by default (``ZAPIER_PARSER_ENABLED=false``). There is no Zapier API client: the only thing
that crosses to Zapier is an email, sent through the Graph draft/send path, and the only thing
Zapier does with it is upsert a row in the operator's Sheet. The system of record never depends
on it. See docs/integrations/zapier.md.
"""

from __future__ import annotations

from wholesaling.integrations.zapier.parser_email import (
    REPLAY_KEY_LABELS,
    ROW_KEY_LABEL,
    ParsedParserEmail,
    parse_parser_email,
    parser_template,
)
from wholesaling.integrations.zapier.send import ParserSendOutcome, send_parser_email_via_graph

__all__ = [
    "REPLAY_KEY_LABELS",
    "ROW_KEY_LABEL",
    "ParsedParserEmail",
    "ParserSendOutcome",
    "parse_parser_email",
    "parser_template",
    "send_parser_email_via_graph",
]
