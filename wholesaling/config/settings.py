"""Runtime settings, loaded from environment / .env.

Every integration has a ``*_MODE`` switch. ``mock`` is the default everywhere so a fresh
checkout runs with no credentials and no paid services (REQUIREMENTS.md RUL-01, RUL-02).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class ServiceRole(StrEnum):
    API = "api"
    WORKER = "worker"


Mode = Literal["mock", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Runtime
    app_env: AppEnv = AppEnv.LOCAL
    log_level: str = "INFO"
    service_role: ServiceRole = ServiceRole.API
    port: int = 8000

    # Database
    database_url: str = "sqlite+aiosqlite:///./local.db"
    test_database_url: str | None = None

    # Temporal
    temporal_mode: Mode = "mock"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: SecretStr | None = None
    temporal_tls: bool = False
    temporal_tls_client_cert_path: str | None = None
    temporal_tls_client_key_path: str | None = None
    temporal_task_queue: str = "wholesaling-main"

    # Model provider
    model_provider: Literal["mock", "anthropic", "openai-compatible"] = "mock"
    anthropic_api_key: SecretStr | None = None
    model_classify: str = "claude-haiku-5-5"
    model_extract: str = "claude-haiku-5-5"
    model_draft: str = "claude-sonnet-5-5"
    model_qa: str = "claude-sonnet-5-5"
    model_comp_audit: str = "claude-sonnet-5-5"

    # Microsoft Graph
    graph_mode: Mode = "mock"
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: SecretStr | None = None
    graph_mailbox: str | None = None
    graph_webhook_url: str | None = None
    graph_webhook_client_state: SecretStr | None = None
    # RUL-06: no live messages in this build. The live gateway refuses send_draft unless true.
    graph_allow_live_send: bool = False

    # Data providers: deeds, market signals, contact enrichment (INT-03 OPEN; only mock exists)
    data_provider_mode: Mode = "mock"

    # Google Sheets
    sheets_mode: Mode = "mock"
    google_service_account_json: str | None = None
    sheets_spreadsheet_id: str | None = None

    # Zapier parser (optional path, default off)
    zapier_parser_enabled: bool = False
    zapier_parser_address: str | None = None

    # SMS: never enabled in this build
    sms_mode: Literal["off", "mock", "live"] = "off"
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_messaging_service_sid: str | None = None

    # DocuSign
    docusign_mode: Mode = "mock"
    docusign_integration_key: str | None = None
    docusign_user_id: str | None = None
    docusign_account_id: str | None = None
    docusign_private_key_path: str | None = None

    # Blob storage
    blob_mode: Literal["local", "s3"] = "local"
    blob_local_dir: str = "./var/blobs"
    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None

    # Sender identity for outbound mail (CMP-01 CAN-SPAM; PRE-02 address, PRE-16 approval).
    # ``footer()`` in agents/templates refuses to render without a postal address and opt-out line.
    sender_name: str = "Aaron"
    sender_company: str = "NorthStack"
    sender_postal_address: str | None = None
    opt_out_text: str = (
        'If you would rather not hear from us, reply with "unsubscribe" and we will remove you.'
    )

    # Business-hour send window in the market's local time (INV-12; values approved under PRE-16).
    send_window_start_hour: int = Field(default=9, ge=0, le=23)
    send_window_end_hour: int = Field(default=17, ge=1, le=24)
    send_window_weekdays_only: bool = True

    # Alerting
    alert_email_to: str | None = None
    slack_webhook_url: SecretStr | None = None

    # Budget
    soft_budget_cap_usd: float = Field(default=1500, ge=0)
    soft_budget_alert_pct: int = Field(default=80, ge=1, le=100)

    # Operator surface (BO-07 review queue / policy page / daily digest). Single operator,
    # HTTP Basic. The hash is produced by ``wholesaling.api.auth.hash_password`` (PBKDF2-HMAC);
    # see docs/operator/review-queue.md. Without a hash every /review and /policies call is 503.
    operator_username: str = "operator"
    operator_password_hash: SecretStr | None = None
    # Daily digest: fires at this local hour in the operator's timezone (WF-18 rejected a fixed
    # 07:00 review time; the review queue itself is never time-gated).
    digest_hour_local: int = Field(default=7, ge=0, le=23)
    operator_timezone: str = "America/Chicago"

    @field_validator("sms_mode")
    @classmethod
    def _sms_never_live_in_this_build(cls, v: str) -> str:
        # REQUIREMENTS.md RUL-06: no SMS at any point in this build.
        if v == "live":
            msg = "SMS_MODE=live is not permitted in this build (REQUIREMENTS.md RUL-06)"
            raise ValueError(msg)
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.app_env == AppEnv.PRODUCTION

    def validate_for_production(self) -> list[str]:
        """Return a list of problems that must be fixed before running in production."""
        problems: list[str] = []
        if self.is_sqlite:
            problems.append("DATABASE_URL must be PostgreSQL in production")
        if self.temporal_mode != "live":
            problems.append("TEMPORAL_MODE must be 'live' in production")
        has_mtls = (
            self.temporal_tls_client_cert_path is not None
            and self.temporal_tls_client_key_path is not None
        )
        if not self.temporal_tls or (self.temporal_api_key is None and not has_mtls):
            problems.append(
                "Temporal Cloud requires TEMPORAL_TLS=true and either TEMPORAL_API_KEY "
                "or both TEMPORAL_TLS_CLIENT_CERT_PATH and TEMPORAL_TLS_CLIENT_KEY_PATH"
            )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
