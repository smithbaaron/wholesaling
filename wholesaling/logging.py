"""Structured logging via structlog.

- JSON lines in staging/production, coloured console output in local/test.
- ``bind_correlation_id`` puts a ``correlation_id`` into the context so every subsequent log
  line from the same task carries it (Temporal activities bind the workflow id).
- Secrets never reach a log sink: any key containing SECRET, TOKEN, KEY, or PASSWORD is
  redacted before rendering (REQUIREMENTS.md XC-02 asks for logging; RUL-01 for no leaks).
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from wholesaling.config.settings import AppEnv, Settings

REDACTED = "***REDACTED***"
_SENSITIVE_MARKERS = ("SECRET", "TOKEN", "KEY", "PASSWORD")
_configured = False


def is_sensitive_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SENSITIVE_MARKERS)


def redact(value: Any) -> Any:
    """Recursively redact sensitive keys in mappings (and mappings nested in lists)."""
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    return value


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor: scrub sensitive keys anywhere in the event dict."""
    for key in list(event_dict):
        if is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact(event_dict[key])
    return event_dict


def _use_json(settings: Settings) -> bool:
    return settings.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION)


def configure_logging(settings: Settings, *, force: bool = False) -> None:
    """Configure structlog and the stdlib root logger. Idempotent unless ``force``."""
    global _configured
    if _configured and not force:
        return

    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
    ]
    renderer: Any
    if _use_json(settings):
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Safe to call before ``configure_logging``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation id into the structlog context for the current task; returns it."""
    cid = correlation_id or uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(correlation_id=cid)
    return cid


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def current_context() -> MutableMapping[str, Any]:
    return dict(structlog.contextvars.get_contextvars())
