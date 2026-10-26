"""Object storage for seller photos (REQUIREMENTS.md SEL-12, INT-26, ENG-Q9).

``get_blob_storage(settings)`` returns ``LocalDirStorage`` for ``BLOB_MODE=local`` (the
default; a Railway Volume in deployment) and raises for ``s3`` until that implementation exists.
Tests use ``MockBlobStorage``.
"""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.blob.interface import (
    BlobKeyError,
    BlobNotFound,
    BlobStorage,
    StoredBlob,
    validate_key,
)
from wholesaling.integrations.blob.local import LocalDirStorage
from wholesaling.integrations.blob.mock import MockBlobStorage


def get_blob_storage(settings: Settings) -> BlobStorage:
    if settings.blob_mode == "local":
        return LocalDirStorage(settings.blob_local_dir)
    from wholesaling.integrations.blob.s3 import S3BlobStorage

    return S3BlobStorage(settings)  # raises NotImplementedError (documented stub)


__all__ = [
    "BlobKeyError",
    "BlobNotFound",
    "BlobStorage",
    "LocalDirStorage",
    "MockBlobStorage",
    "StoredBlob",
    "get_blob_storage",
    "validate_key",
]
