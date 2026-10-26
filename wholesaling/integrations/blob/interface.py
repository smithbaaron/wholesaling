"""``BlobStorage``: where seller photos live (REQUIREMENTS.md SEL-12, INT-26, ENG-Q9, RUL-02).

ENG-Q9 (accepted as recommended): a Railway Volume behind this interface for the pilot
(``LocalDirStorage``), an S3-compatible store (Cloudflare R2) as the production implementation
when chosen, and an in-memory mock for tests. Keys are opaque, slash-separated paths chosen by
the caller (``sell/<submission id>/<n>.<ext>``); the store never interprets them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from wholesaling.integrations.base import HealthStatus, IntegrationMode

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


class BlobKeyError(ValueError):
    """A key that could escape the store's root or is otherwise malformed."""


class BlobNotFound(LookupError):
    """``get`` on a key the store does not hold."""


def validate_key(key: str) -> str:
    """Reject empty keys, absolute paths, ``..`` segments and odd characters."""
    if not key or len(key) > 512 or not _KEY_RE.match(key) or ".." in key.split("/"):
        msg = f"invalid blob key {key!r}"
        raise BlobKeyError(msg)
    return key


@dataclass(frozen=True, slots=True)
class StoredBlob:
    key: str
    size: int
    content_type: str
    sha256: str
    stored_at: datetime


@runtime_checkable
class BlobStorage(Protocol):
    name: str
    mode: IntegrationMode

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        """Store ``data`` under ``key`` (overwrites). Returns the record the caller persists."""
        ...

    async def get(self, key: str) -> bytes:
        """The bytes behind ``key``; raises ``BlobNotFound``."""
        ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> bool:
        """Remove ``key``; ``False`` when it was not there."""
        ...

    async def healthcheck(self) -> HealthStatus: ...


__all__ = ["BlobKeyError", "BlobNotFound", "BlobStorage", "StoredBlob", "validate_key"]
