# Wholesaling Automation

Production real estate wholesaling automation: a set of Temporal workflows on Railway workers that
call a provider-agnostic model API, with PostgreSQL as the system of record and Google Sheets as the
operator's view.

**Read first:** [REQUIREMENTS.md](REQUIREMENTS.md) is the build contract. Every feature traces to a
row there. The three source documents sit beside it.

## Status

Build order step 1 to 3 (Foundation). Nothing is deployed. No integration is live. Every external
service runs in `mock` mode until its human-action checkpoint in [docs/CHECKPOINTS.md](docs/CHECKPOINTS.md)
is cleared.

## Quick start (no services needed)

```bash
uv sync
uv run pytest
uv run ruff check . && uv run mypy
```

With a local Temporal dev server and the API:

```bash
temporal server start-dev
uv run wholesaling-worker
uv run wholesaling-api
```

See [SETUP.md](SETUP.md) for the full environment and checkpoint walkthrough.

## Layout

See [docs/adr/0001-repo-layout-and-toolchain.md](docs/adr/0001-repo-layout-and-toolchain.md).

## Rules that never change

- No live messages, purchases, SMS, contracts, or earnest money in this build (REQUIREMENTS.md RUL-06).
- Every outbound action runs the same code path; the policy table decides send vs queue_for_human.
  Every action starts at L0 (REQUIREMENTS.md POL-01, POL-02).
- Postgres is the only writer of truth. Agents never write to Sheets (STK-02).
