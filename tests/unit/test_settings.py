"""Settings contract: mock-by-default, SMS never live, .env.example parity."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from wholesaling.config.settings import AppEnv, ServiceRole, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def test_defaults_are_mock_and_local() -> None:
    s = _settings()
    assert s.app_env == AppEnv.LOCAL
    assert s.service_role == ServiceRole.API
    assert s.graph_mode == "mock"
    assert s.sheets_mode == "mock"
    assert s.docusign_mode == "mock"
    assert s.model_provider == "mock"
    assert s.sms_mode == "off"
    assert s.zapier_parser_enabled is False  # RUL-07
    assert s.is_sqlite


def test_sms_live_is_rejected_in_this_build() -> None:
    # REQUIREMENTS.md RUL-06
    with pytest.raises(ValidationError):
        _settings(sms_mode="live")


def test_sms_mock_is_allowed_but_off_by_default() -> None:
    assert _settings(sms_mode="mock").sms_mode == "mock"


def test_production_validation_flags_sqlite_and_missing_temporal_cloud() -> None:
    s = _settings(app_env="production")
    problems = s.validate_for_production()
    assert any("PostgreSQL" in p for p in problems)
    assert any("Temporal Cloud" in p for p in problems)


def test_production_validation_passes_with_postgres_and_cloud() -> None:
    s = _settings(
        app_env="production",
        database_url="postgresql+asyncpg://u:p@h/db",
        temporal_tls="true",
        temporal_api_key="k",
    )
    assert s.validate_for_production() == []


def test_env_example_keys_match_settings_fields() -> None:
    """Every key in .env.example is a Settings field, and every field is documented there."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set()
    for line in text.splitlines():
        m = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=", line)
        if m:
            documented.add(m.group(1).lower())
    fields = set(Settings.model_fields)
    missing_in_settings = documented - fields
    missing_in_example = fields - documented
    assert not missing_in_settings, (
        f".env.example keys unknown to Settings: {missing_in_settings}"
    )
    assert not missing_in_example, (
        f"Settings fields missing from .env.example: {missing_in_example}"
    )
