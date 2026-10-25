# ADR 0002 — Postgres in production, SQLite for service-free tests

Date: 2026-10-25. Status: accepted.

## Context

REQUIREMENTS.md ENG-Q4 says unit tests run with no services. The development machine has no Docker
and no local Postgres. The schema acceptance tests (BG Step 2: duplicate, update, correction,
conflicting update) need a real database.

## Decision

- Models use portable types (`wholesaling/db/types.py`): `GUID` (native UUID on Postgres, CHAR(36)
  elsewhere), `JSONDoc` (JSONB on Postgres, JSON elsewhere), `Numeric` for money, timezone-aware
  `DateTime`.
- The test suite runs on SQLite in memory by default and on Postgres when `TEST_DATABASE_URL` is a
  Postgres URL. CI runs both. Tests that need Postgres-only behaviour are marked `postgres`.
- Upserts are implemented in repositories as select-then-insert/update inside a transaction with
  unique constraints as the backstop, not with dialect-specific `ON CONFLICT`.
- Alembic migrations use `render_as_batch=True` so they apply on SQLite too.

## Consequences

- No partial indexes, exclusion constraints, or Postgres triggers in the core schema. If one is
  ever needed it is added in a Postgres-only migration step guarded by dialect and tested under the
  `postgres` marker.
- Concurrency-sensitive upserts (two workers upserting the same buy box) are covered by a unique
  constraint on `(investor_id, buy_box_key, version)` and by Temporal's single-workflow-per-id rule;
  the Postgres CI job exercises the real locking behaviour.
