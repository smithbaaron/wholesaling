"""agent_runs.cost_usd: NUMERIC(12,2) -> NUMERIC(12,6).

A single Haiku classification costs about $0.0003; at two decimals every such run was
recorded as $0.00 and the budget check (WF-16) would never see model spend accumulate.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.alter_column(
            "cost_usd",
            existing_type=sa.Numeric(precision=12, scale=2),
            type_=sa.Numeric(precision=12, scale=6),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.alter_column(
            "cost_usd",
            existing_type=sa.Numeric(precision=12, scale=6),
            type_=sa.Numeric(precision=12, scale=2),
            existing_nullable=False,
        )
