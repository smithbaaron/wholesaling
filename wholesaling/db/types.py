"""Portable column types.

Production and CI run PostgreSQL. Unit tests run SQLite in memory so a fresh checkout needs no
services (docs/adr/0002-portable-schema.md). These decorators pick the native Postgres type when
available and a portable fallback otherwise. Never use dialect-specific columns directly in models.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CHAR, JSON, Numeric
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine


class GUID(TypeDecorator[uuid.UUID]):
    """UUID stored natively on Postgres, as CHAR(36) elsewhere. Always returns ``uuid.UUID``."""

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONDoc(TypeDecorator[Any]):
    """JSONB on Postgres, JSON elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


# Money: dollars with cents. Offers are rounded to whole dollars by A7, but ledgers keep cents.
Money = Numeric(12, 2)

# Ratios and percentages stored as decimals (0.70, not 70).
Ratio = Numeric(6, 4)

# Model-API spend: a single Haiku classify costs ~$0.0003, so ledger precision is 6 decimals.
MicroMoney = Numeric(12, 6)
