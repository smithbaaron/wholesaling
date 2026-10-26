"""``contracts``: the BO-12 deal/transaction record (REQUIREMENTS.md SEL-10, SEL-11, BB-11).

One row per agreement: the PSA (deal record with deadlines, earnest money, buyer, deposit,
closing, deal-death) and the Assignment Agreement pointing back through ``parent_contract_id``.
Portable per ADR 0002: enums are VARCHAR + CHECK, batch mode like 0002. tests/db/test_migrations.py
asserts head matches ``Base.metadata``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 01:50:34.355603+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from wholesaling.db.types import GUID, JSONDoc


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contracts",
        sa.Column("offer_id", GUID(), nullable=False),
        sa.Column("seller_lead_id", GUID(), nullable=False),
        sa.Column("parent_contract_id", GUID(), nullable=True),
        sa.Column(
            "type",
            sa.Enum(
                "psa",
                "assignment",
                name="contract_type",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "requested",
                "sent_for_signature",
                "signed",
                "closed",
                "cancelled",
                name="contract_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("property_address_line1", sa.String(length=255), nullable=False),
        sa.Column("property_city", sa.String(length=120), nullable=True),
        sa.Column("property_state", sa.String(length=2), nullable=True),
        sa.Column("property_postal_code", sa.String(length=16), nullable=True),
        sa.Column("purchase_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("docusign_envelope_id", sa.String(length=64), nullable=True),
        sa.Column("envelope_status", sa.String(length=32), nullable=True),
        sa.Column("sent_for_signature_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_by", sa.String(length=120), nullable=True),
        sa.Column("earnest_money_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "earnest_money_status",
            sa.Enum(
                "none",
                "requested",
                "deposited",
                "released",
                name="earnest_money_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("option_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inspection_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closing_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("buyer_investor_id", GUID(), nullable=True),
        sa.Column("assignment_fee", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("realized_assignment_fee", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("deposit_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("deposit_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("title_company", sa.String(length=255), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deal_dead_reason", sa.String(length=255), nullable=True),
        sa.Column("buyer_responses", JSONDoc(), nullable=False),
        sa.Column("checklist", JSONDoc(), nullable=False),
        sa.Column("meta", JSONDoc(), nullable=False),
        sa.Column("id", GUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["buyer_investor_id"],
            ["investors.id"],
            name=op.f("fk_contracts_buyer_investor_id_investors"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_contracts_offer_id_offers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_contract_id"],
            ["contracts.id"],
            name=op.f("fk_contracts_parent_contract_id_contracts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["seller_lead_id"],
            ["seller_leads.id"],
            name=op.f("fk_contracts_seller_lead_id_seller_leads"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contracts")),
        sa.UniqueConstraint("offer_id", "type", name=op.f("uq_contracts_offer_id_type")),
    )
    with op.batch_alter_table("contracts", schema=None) as batch_op:
        batch_op.create_index("ix_contracts_buyer_investor_id", ["buyer_investor_id"], unique=False)
        batch_op.create_index(
            "ix_contracts_parent_contract_id", ["parent_contract_id"], unique=False
        )
        batch_op.create_index("ix_contracts_seller_lead_id", ["seller_lead_id"], unique=False)
        batch_op.create_index("ix_contracts_status", ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("contracts", schema=None) as batch_op:
        batch_op.drop_index("ix_contracts_status")
        batch_op.drop_index("ix_contracts_seller_lead_id")
        batch_op.drop_index("ix_contracts_parent_contract_id")
        batch_op.drop_index("ix_contracts_buyer_investor_id")

    op.drop_table("contracts")
