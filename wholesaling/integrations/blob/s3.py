"""S3-compatible ``BlobStorage`` (Cloudflare R2 / AWS S3): the production implementation ENG-Q9
names for when the operator chooses it. **Documented stub**: constructing it raises until the
storage account exists (no credentials are assumed, XC-10 / RUL-01).

To implement: add ``boto3`` (or an ``httpx``-based SigV4 client to avoid the dependency) as an
optional extra, read ``S3_ENDPOINT_URL`` / ``S3_BUCKET`` / ``S3_ACCESS_KEY_ID`` /
``S3_SECRET_ACCESS_KEY`` from ``Settings``, map ``put`` -> ``PutObject`` (``ContentType`` from
the argument, ``ChecksumSHA256`` from the digest), ``get`` -> ``GetObject``, ``exists`` ->
``HeadObject`` (404 -> False), ``delete`` -> ``DeleteObject``, and ``healthcheck`` ->
``HeadBucket``. Keys map 1:1 onto object keys; ``validate_key`` already constrains them to a
safe character set. Then flip ``BLOB_MODE=s3`` in Railway and the web form needs no change.
"""

from __future__ import annotations

from wholesaling.config.settings import Settings
from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.blob.interface import StoredBlob

_NOT_IMPLEMENTED = "S3BlobStorage is a documented stub (ENG-Q9); it cannot be constructed yet"


class S3BlobStorage:
    name = "blob"
    mode: IntegrationMode = "live"

    def __init__(self, settings: Settings) -> None:
        missing = [
            k
            for k, v in (
                ("S3_ENDPOINT_URL", settings.s3_endpoint_url),
                ("S3_BUCKET", settings.s3_bucket),
                ("S3_ACCESS_KEY_ID", settings.s3_access_key_id),
                ("S3_SECRET_ACCESS_KEY", settings.s3_secret_access_key),
            )
            if not v
        ]
        detail = f" (missing {', '.join(missing)})" if missing else ""
        msg = (
            "BLOB_MODE=s3 is not implemented yet: the S3-compatible store is the ENG-Q9 "
            f"production option and waits on a storage account{detail}. Use BLOB_MODE=local."
        )
        raise NotImplementedError(msg)

    # The Protocol surface, so the factory's return type holds once the body exists.
    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredBlob:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get(self, key: str) -> bytes:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def exists(self, key: str) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def delete(self, key: str) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def healthcheck(self) -> HealthStatus:
        raise NotImplementedError(_NOT_IMPLEMENTED)


__all__ = ["S3BlobStorage"]
