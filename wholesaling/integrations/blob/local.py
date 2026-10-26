"""``LocalDirStorage``: blobs as files under ``BLOB_LOCAL_DIR`` (ENG-Q9 pilot choice: a Railway
Volume mounted at that path). Writes are atomic (temp file + rename) so a crash mid-upload
never leaves a half photo behind the key."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.blob.interface import BlobNotFound, StoredBlob, validate_key

_META_SUFFIX = ".meta.json"


class LocalDirStorage:
    name = "blob"
    mode: IntegrationMode = "live"  # a real filesystem, not a stub

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / validate_key(key)).resolve()
        if self.root not in path.parents:
            msg = f"key {key!r} escapes the storage root"
            raise ValueError(msg)
        return path

    def _put_sync(self, key: str, data: bytes, content_type: str) -> StoredBlob:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        record = StoredBlob(
            key=key,
            size=len(data),
            content_type=content_type,
            sha256=digest,
            stored_at=datetime.now(tz=UTC),
        )
        path.with_name(path.name + _META_SUFFIX).write_text(
            json.dumps(
                {
                    "content_type": content_type,
                    "sha256": digest,
                    "size": len(data),
                    "stored_at": record.stored_at.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return record

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        return await asyncio.to_thread(self._put_sync, key, data, content_type)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise BlobNotFound(key)
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        path.unlink()
        path.with_name(path.name + _META_SUFFIX).unlink(missing_ok=True)
        return True

    async def healthcheck(self) -> HealthStatus:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            ok = os.access(self.root, os.W_OK)
        except OSError:
            ok = False
        return HealthStatus(name=self.name, mode=self.mode, ok=ok, detail=str(self.root))


__all__ = ["LocalDirStorage"]
