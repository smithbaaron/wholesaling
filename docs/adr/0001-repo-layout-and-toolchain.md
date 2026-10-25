# ADR 0001 — Repository layout and toolchain

Date: 2026-10-25. Status: accepted (REQUIREMENTS.md ENG-Q1..Q6, Q13, approved by the user).

## Decision

One installable package, `wholesaling/`, with subpackages `config`, `db`, `api`, `workflows`,
`activities`, `workers`, `integrations`, `agents`, `policy`. Top-level `config/` (YAML tunables),
`tests/`, `infra/`, `docs/`, `scripts/`.

Toolchain: Python 3.12, `uv` + `pyproject.toml` + `uv.lock`, `ruff` (lint + format), `mypy --strict`,
`pytest` + `pytest-asyncio`, SQLAlchemy 2 async + `asyncpg` + Alembic, `pydantic-settings` for env,
YAML for tunables, Jinja2 + HTMX for the operator pages.

## Why

- v2 fixes the stack (FastAPI, Postgres, Temporal, Railway) and names `agents/llm.py`, but nothing
  else about layout. The v1 build guide's flat folders map one-to-one onto subpackages, which keeps a
  single import root and a single Docker image for both Railway services.
- Workflows and activities are separate packages so the Temporal determinism rule (no I/O in
  workflow code) is enforceable by import-linting later.
- Every integration subpackage has `interface.py`, `mock.py`, `real.py` so mocks are first-class
  and the synthetic pilot runs with no credentials.

## Consequences

- One Docker image; `SERVICE_ROLE=api|worker` selects the entrypoint on Railway.
- `mypy --strict` from day one; migrations are excluded.
