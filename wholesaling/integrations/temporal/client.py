"""Live Temporal gateway backed by ``temporalio.client.Client``.

Connection options come from settings:

- ``TEMPORAL_ADDRESS`` / ``TEMPORAL_NAMESPACE``
- ``TEMPORAL_TLS=true`` enables TLS. With ``TEMPORAL_API_KEY`` set this is Temporal Cloud's
  API-key auth; with ``TEMPORAL_TLS_CLIENT_CERT_PATH`` + ``TEMPORAL_TLS_CLIENT_KEY_PATH`` it is
  mTLS. Both may be absent for a TLS-terminating proxy in front of a self-hosted server.
- ``TEMPORAL_TLS=false`` is the local ``temporal server start-dev`` case.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from temporalio.client import Client
from temporalio.service import TLSConfig

from wholesaling.config.settings import Settings
from wholesaling.integrations.base import HealthStatus, IntegrationMode
from wholesaling.integrations.temporal.interface import WorkflowHandleInfo
from wholesaling.logging import get_logger

log = get_logger(__name__)


def build_tls_config(settings: Settings) -> TLSConfig | bool:
    """Translate settings into the ``tls`` argument of ``Client.connect``."""
    if not settings.temporal_tls:
        return False
    cert_path = settings.temporal_tls_client_cert_path
    key_path = settings.temporal_tls_client_key_path
    if cert_path and key_path:
        return TLSConfig(
            client_cert=Path(cert_path).read_bytes(),
            client_private_key=Path(key_path).read_bytes(),
        )
    if bool(cert_path) != bool(key_path):
        msg = "TEMPORAL_TLS_CLIENT_CERT_PATH and TEMPORAL_TLS_CLIENT_KEY_PATH must be set together"
        raise ValueError(msg)
    return True


async def connect_client(settings: Settings) -> Client:
    """Connect a raw SDK client (used by the worker and by the live gateway)."""
    api_key = settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        api_key=api_key,
        tls=build_tls_config(settings),
    )


class LiveTemporalGateway:
    """``TemporalGateway`` implementation over a real Temporal server."""

    name = "temporal"
    mode: IntegrationMode = "live"

    def __init__(self, settings: Settings, client: Client | None = None) -> None:
        self._settings = settings
        self._client: Client | None = client

    @property
    def client(self) -> Client:
        if self._client is None:
            msg = "Temporal gateway is not connected; call connect() first"
            raise RuntimeError(msg)
        return self._client

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = await connect_client(self._settings)
        log.info(
            "temporal.connected",
            address=self._settings.temporal_address,
            namespace=self._settings.temporal_namespace,
            tls=self._settings.temporal_tls,
        )

    async def close(self) -> None:
        # The Python SDK client holds no resources that need an explicit close; the underlying
        # Core connection is dropped with the object. Keep the hook so callers are symmetric.
        self._client = None

    async def start_workflow(
        self,
        workflow: str,
        args: Sequence[Any],
        *,
        workflow_id: str,
        task_queue: str | None = None,
    ) -> WorkflowHandleInfo:
        queue = task_queue or self._settings.temporal_task_queue
        handle = await self.client.start_workflow(
            workflow, args=list(args), id=workflow_id, task_queue=queue
        )
        return WorkflowHandleInfo(
            workflow_id=handle.id, run_id=handle.result_run_id, task_queue=queue
        )

    async def signal(self, workflow_id: str, signal: str, *args: Any) -> None:
        await self.client.get_workflow_handle(workflow_id).signal(signal, args=list(args))

    async def query(self, workflow_id: str, query: str, *args: Any) -> Any:
        return await self.client.get_workflow_handle(workflow_id).query(query, args=list(args))

    async def healthcheck(self) -> HealthStatus:
        try:
            if self._client is None:
                await self.connect()
            ok = await self.client.service_client.check_health()
        except Exception as exc:  # health must never raise
            return HealthStatus(name=self.name, mode=self.mode, ok=False, detail=str(exc))
        detail = "" if ok else "temporal service reported unhealthy"
        return HealthStatus(name=self.name, mode=self.mode, ok=ok, detail=detail)
