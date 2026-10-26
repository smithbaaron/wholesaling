"""In-memory ``BlobStorage`` for tests and the synthetic pilot. Records every call."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.blob.interface import BlobNotFound, StoredBlob, validate_key


@dataclass(slots=True)
class MockBlobStorage:
    name: str = "blob"
    mode: IntegrationMode = "mock"
    blobs: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    healthy: bool = True
    fail_next_put: Exception | None = None

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        validate_key(key)
        self.calls.append({"method": "put", "key": key, "size": len(data)})
        if self.fail_next_put is not None:
            exc, self.fail_next_put = self.fail_next_put, None
            raise exc
        self.blobs[key] = (bytes(data), content_type)
        return StoredBlob(
            key=key,
            size=len(data),
            content_type=content_type,
            sha256=hashlib.sha256(data).hexdigest(),
            stored_at=datetime.now(tz=UTC),
        )

    async def get(self, key: str) -> bytes:
        self.calls.append({"method": "get", "key": key})
        try:
            return self.blobs[key][0]
        except KeyError as exc:
            raise BlobNotFound(key) from exc

    async def exists(self, key: str) -> bool:
        return key in self.blobs

    async def delete(self, key: str) -> bool:
        self.calls.append({"method": "delete", "key": key})
        return self.blobs.pop(key, None) is not None

    async def healthcheck(self) -> HealthStatus:
        return HealthStatus(
            name=self.name, mode=self.mode, ok=self.healthy, detail=f"{len(self.blobs)} blobs"
        )

    def reset(self) -> None:
        self.blobs.clear()
        self.calls.clear()


__all__ = ["MockBlobStorage"]
