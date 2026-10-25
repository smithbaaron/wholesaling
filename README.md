# Wholesaling Automation

Production real estate wholesaling automation: a set of Temporal workflows on Railway workers that
call a provider-agnostic model API, with PostgreSQL as the system of record and Google Sheets as the
operator's view.

**Read first:** [REQUIREMENTS.md](REQUIREMENTS.md) is the build contract. Every feature traces to a
row there. The three source documents sit beside it.

## Status

Build order steps 1 to 3 (Foundation) are in progress in the cloud session that took over from
[HANDOFF.md](HANDOFF.md):

| Step | State |
|---|---|
| BO-01 Repo, CI, `.env.example`, requirements matrix, deployment and docs artifacts | done on disk: CI workflow, Dockerfile, `.railway/railway.py`, docker-compose, SETUP, CHECKPOINTS, RUNBOOK. Not pushed: no Git remote yet (ENG-Q12). |
| BO-02 Postgres schema and migrations | in progress (`wholesaling/db/models`, repositories, Alembic) |
| BO-03 Temporal worker, HelloWorkflow, health endpoint, Railway deploy | code in progress (`wholesaling/api`, `workers`, `workflows`, `activities`, `integrations/temporal`); Railway deploy blocked on checkpoints PRE-07 and PRE-08 |

Nothing is deployed. No integration is live. Every external service runs in `mock` mode until its
human-action checkpoint in [docs/CHECKPOINTS.md](docs/CHECKPOINTS.md) is cleared, and no 24/7
claim is made until the restart test in [docs/RUNBOOK.md](docs/RUNBOOK.md) passes (RUL-09).

## Quick start

Full walkthrough, Windows and macOS/Linux, in [SETUP.md](SETUP.md). The short version, no services
needed:

```bash
uv sync --frozen
cp .env.example .env
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Local Temporal dev server, worker and API (three terminals; everything external stays mocked):

```bash
temporal server start-dev --db-filename var/temporal.db
TEMPORAL_MODE=live SERVICE_ROLE=worker uv run wholesaling-worker
TEMPORAL_MODE=live SERVICE_ROLE=api    uv run wholesaling-api     # http://localhost:8000/health
```

Docker is optional; `docker-compose.yml` gives local Postgres + Temporal for those who have it.

Deploying and operating: [docs/RUNBOOK.md](docs/RUNBOOK.md). What you must do by hand before any
integration goes live: [docs/CHECKPOINTS.md](docs/CHECKPOINTS.md).

## Layout

See [docs/adr/0001-repo-layout-and-toolchain.md](docs/adr/0001-repo-layout-and-toolchain.md).

## Rules that never change

- No live messages, purchases, SMS, contracts, or earnest money in this build (REQUIREMENTS.md RUL-06).
- Every outbound action runs the same code path; the policy table decides send vs queue_for_human.
  Every action starts at L0 (REQUIREMENTS.md POL-01, POL-02).
- Postgres is the only writer of truth. Agents never write to Sheets (STK-02).
