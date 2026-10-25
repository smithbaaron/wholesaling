"""Initial schema: the 20 tables of REQUIREMENTS.md DM-01/DM-05/DM-06, plus the 12 policy rows at L0.

Hand-reviewed autogenerate output. Portable per docs/adr/0002-portable-schema.md: no ON CONFLICT,
no partial indexes, no Postgres-only types; enums are VARCHAR + CHECK. tests/db/test_migrations.py
asserts this migration matches ``Base.metadata`` exactly.

Revision ID: 0001
Revises:
Create Date: 2026-10-25 21:50:25.489071+00:00

"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from wholesaling.db.base import utcnow
from wholesaling.db.types import GUID, JSONDoc
from wholesaling.policy.actions import PolicyAction, default_level


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column(
            "agent",
            sa.Enum(
                "M1",
                "A1",
                "A2",
                "A3",
                "A4",
                "A5",
                "A6",
                "A7",
                "D1",
                "D2",
                "S1",
                "S2",
                "S3",
                "S4",
                "S5",
                "S6",
                name="agent_id",
                native_enum=False,
                create_constraint=True,
                length=4,
            ),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("output", JSONDoc(), nullable=False),
        sa.Column("decision", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index("ix_agent_runs_agent", "agent_runs", ["agent"], unique=False)
    op.create_index("ix_agent_runs_correlation_id", "agent_runs", ["correlation_id"], unique=False)

    op.create_table(
        "audit_events",
        sa.Column(
            "actor",
            sa.Enum(
                "system",
                "human",
                "agent",
                name="actor",
                native_enum=False,
                create_constraint=True,
                length=8,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=120), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", GUID(), nullable=True),
        sa.Column("payload", JSONDoc(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", GUID(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_audit_events_idempotency_key")),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"], unique=False)
    op.create_index(
        "ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"], unique=False
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"], unique=False)

    op.create_table(
        "markets",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("state", sa.String(length=2), nullable=False),
        sa.Column(
            "tier",
            sa.Enum(
                "Active",
                "Watch",
                "Excluded",
                name="market_tier",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("deal_band_min", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("deal_band_max", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("score", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("signals", JSONDoc(), nullable=False),
        sa.Column("last_scored_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_markets")),
        sa.UniqueConstraint("name", "state", name=op.f("uq_markets_name_state")),
    )
    op.create_index("ix_markets_tier", "markets", ["tier"], unique=False)

    op.create_table(
        "policies",
        sa.Column(
            "action",
            sa.Enum(
                "investor_initial_outreach",
                "investor_followup",
                "investor_reply",
                "seller_outreach_email",
                "seller_outreach_sms",
                "seller_intake_reply",
                "offer_send",
                "counter_acceptance",
                "contract_execution",
                "earnest_money",
                "buyer_disposition_blast",
                "opt_out_suppression",
                name="policy_action",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "level",
            sa.Enum(
                "L0",
                "L1",
                "L2",
                name="autonomy_level",
                native_enum=False,
                create_constraint=True,
                length=4,
            ),
            nullable=False,
        ),
        sa.Column("daily_cap", sa.Integer(), nullable=True),
        sa.Column("threshold", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policies")),
        sa.UniqueConstraint("action", name=op.f("uq_policies_action")),
    )
    op.create_table(
        "investors",
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("entity_key", sa.String(length=255), nullable=False),
        sa.Column("aliases", JSONDoc(), nullable=False),
        sa.Column(
            "investor_type",
            sa.Enum(
                "flipper",
                "buy_and_hold",
                "wholesaler",
                "ibuyer_institutional",
                "agent_investor",
                "unknown",
                name="investor_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("is_entity", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_breakdown", JSONDoc(), nullable=False),
        sa.Column("market_id", GUID(), nullable=True),
        sa.Column("mailing_address_line1", sa.String(length=255), nullable=True),
        sa.Column("mailing_address_line2", sa.String(length=255), nullable=True),
        sa.Column("mailing_city", sa.String(length=120), nullable=True),
        sa.Column("mailing_state", sa.String(length=2), nullable=True),
        sa.Column("mailing_postal_code", sa.String(length=16), nullable=True),
        sa.Column("mailing_address_key", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=120), nullable=True),
        sa.Column("purchase_count_24m", sa.Integer(), nullable=True),
        sa.Column("last_purchase_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_refs", JSONDoc(), nullable=False),
        sa.Column("excluded_reason", sa.String(length=255), nullable=True),
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
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)", name=op.f("ck_investors_score_range")
        ),
        sa.ForeignKeyConstraint(
            ["market_id"],
            ["markets.id"],
            name=op.f("fk_investors_market_id_markets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investors")),
        sa.UniqueConstraint("entity_key", name=op.f("uq_investors_entity_key")),
    )
    op.create_index("ix_investors_investor_type", "investors", ["investor_type"], unique=False)
    op.create_index("ix_investors_market_id", "investors", ["market_id"], unique=False)
    op.create_index("ix_investors_score", "investors", ["score"], unique=False)

    op.create_table(
        "seller_leads",
        sa.Column("market_id", GUID(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "queued",
                "in_sequence",
                "replied",
                "intake",
                "offered",
                "under_contract",
                "closed",
                "dead",
                "suppressed",
                name="seller_lead_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("property_address_line1", sa.String(length=255), nullable=False),
        sa.Column("property_city", sa.String(length=120), nullable=True),
        sa.Column("property_state", sa.String(length=2), nullable=True),
        sa.Column("property_postal_code", sa.String(length=16), nullable=True),
        sa.Column("address_key", sa.String(length=255), nullable=True),
        sa.Column("parcel_id", sa.String(length=64), nullable=True),
        sa.Column("parcel_verified", sa.Boolean(), nullable=False),
        sa.Column("signals", JSONDoc(), nullable=False),
        sa.Column("signal_score", sa.Integer(), nullable=True),
        sa.Column("assessed_value", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("estimated_value", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("source_refs", JSONDoc(), nullable=False),
        sa.Column("contact_channels", JSONDoc(), nullable=False),
        sa.Column("web_form_submission_id", sa.String(length=128), nullable=True),
        sa.Column("last_contacted_at", sa.DateTime(timezone=True), nullable=True),
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
            ["market_id"],
            ["markets.id"],
            name=op.f("fk_seller_leads_market_id_markets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seller_leads")),
    )
    op.create_index("ix_seller_leads_address_key", "seller_leads", ["address_key"], unique=False)
    op.create_index("ix_seller_leads_market_id", "seller_leads", ["market_id"], unique=False)
    op.create_index("ix_seller_leads_parcel_id", "seller_leads", ["parcel_id"], unique=False)
    op.create_index("ix_seller_leads_status", "seller_leads", ["status"], unique=False)

    op.create_table(
        "contacts",
        sa.Column("investor_id", GUID(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum(
                "email",
                "phone",
                "address",
                name="contact_channel",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("value", sa.String(length=512), nullable=False),
        sa.Column("normalized_value", sa.String(length=512), nullable=False),
        sa.Column("provenance", sa.String(length=255), nullable=True),
        sa.Column("provenance_url", sa.Text(), nullable=True),
        sa.Column("use_limitations", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
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
            ["investor_id"],
            ["investors.id"],
            name=op.f("fk_contacts_investor_id_investors"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
        sa.UniqueConstraint(
            "channel", "normalized_value", name=op.f("uq_contacts_channel_normalized_value")
        ),
    )
    op.create_index("ix_contacts_investor_id", "contacts", ["investor_id"], unique=False)

    op.create_table(
        "intakes",
        sa.Column("seller_lead_id", GUID(), nullable=False),
        sa.Column("motivation", sa.Text(), nullable=True),
        sa.Column(
            "condition_class",
            sa.Enum(
                "Light",
                "Medium",
                "Heavy",
                "Gut",
                name="condition_class",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("disclosed_issues", JSONDoc(), nullable=False),
        sa.Column(
            "timeline",
            sa.Enum(
                "asap",
                "within_30_days",
                "within_90_days",
                "flexible",
                "unknown",
                name="intake_timeline",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("timeline_text", sa.String(length=255), nullable=True),
        sa.Column("asking_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("photos", JSONDoc(), nullable=False),
        sa.Column("photo_link_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("co_owners", sa.Text(), nullable=True),
        sa.Column("co_owners_unknown", sa.Boolean(), nullable=False),
        sa.Column("occupancy", sa.String(length=64), nullable=True),
        sa.Column("occupancy_unknown", sa.Boolean(), nullable=False),
        sa.Column("liens", sa.Text(), nullable=True),
        sa.Column("liens_unknown", sa.Boolean(), nullable=False),
        sa.Column("hoa", sa.String(length=255), nullable=True),
        sa.Column("hoa_unknown", sa.Boolean(), nullable=False),
        sa.Column("property_facts", JSONDoc(), nullable=False),
        sa.Column("missing_required", JSONDoc(), nullable=False),
        sa.Column("intake_complete", sa.Boolean(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            ["seller_lead_id"],
            ["seller_leads.id"],
            name=op.f("fk_intakes_seller_lead_id_seller_leads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_intakes")),
    )
    op.create_index("ix_intakes_intake_complete", "intakes", ["intake_complete"], unique=False)
    op.create_index("ix_intakes_seller_lead_id", "intakes", ["seller_lead_id"], unique=False)

    op.create_table(
        "property_candidates",
        sa.Column("market_id", GUID(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("address_line1", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("postal_code", sa.String(length=16), nullable=True),
        sa.Column("address_key", sa.String(length=255), nullable=True),
        sa.Column("parcel_id", sa.String(length=64), nullable=True),
        sa.Column("seller_lead_id", GUID(), nullable=True),
        sa.Column("list_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("est_arv", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("est_repairs", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("est_fees", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("est_assignment_spread", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("liquidity_score", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("flags", JSONDoc(), nullable=False),
        sa.Column("facts", JSONDoc(), nullable=False),
        sa.Column("raw", JSONDoc(), nullable=False),
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
            ["market_id"],
            ["markets.id"],
            name=op.f("fk_property_candidates_market_id_markets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["seller_lead_id"],
            ["seller_leads.id"],
            name=op.f("fk_property_candidates_seller_lead_id_seller_leads"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_property_candidates")),
        sa.UniqueConstraint(
            "source", "external_id", name=op.f("uq_property_candidates_source_external_id")
        ),
    )
    op.create_index(
        "ix_property_candidates_address_key", "property_candidates", ["address_key"], unique=False
    )
    op.create_index(
        "ix_property_candidates_market_id", "property_candidates", ["market_id"], unique=False
    )
    op.create_index(
        "ix_property_candidates_parcel_id", "property_candidates", ["parcel_id"], unique=False
    )

    op.create_table(
        "source_evidence",
        sa.Column("investor_id", GUID(), nullable=True),
        sa.Column("seller_lead_id", GUID(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("ref", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("raw", JSONDoc(), nullable=False),
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
        sa.CheckConstraint(
            "investor_id IS NOT NULL OR seller_lead_id IS NOT NULL",
            name=op.f("ck_source_evidence_has_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["investor_id"],
            ["investors.id"],
            name=op.f("fk_source_evidence_investor_id_investors"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["seller_lead_id"],
            ["seller_leads.id"],
            name=op.f("fk_source_evidence_seller_lead_id_seller_leads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_evidence")),
    )
    op.create_index(
        "ix_source_evidence_investor_id", "source_evidence", ["investor_id"], unique=False
    )
    op.create_index(
        "ix_source_evidence_seller_lead_id", "source_evidence", ["seller_lead_id"], unique=False
    )
    op.create_index(
        "ix_source_evidence_source_ref", "source_evidence", ["source", "ref"], unique=False
    )

    op.create_table(
        "threads",
        sa.Column(
            "channel",
            sa.Enum(
                "email",
                "sms",
                "web_form",
                name="channel",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("external_thread_id", sa.String(length=512), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("investor_id", GUID(), nullable=True),
        sa.Column("seller_lead_id", GUID(), nullable=True),
        sa.Column("counterpart_address", sa.String(length=512), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
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
            ["investor_id"],
            ["investors.id"],
            name=op.f("fk_threads_investor_id_investors"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["seller_lead_id"],
            ["seller_leads.id"],
            name=op.f("fk_threads_seller_lead_id_seller_leads"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_threads")),
        sa.UniqueConstraint(
            "channel", "external_thread_id", name=op.f("uq_threads_channel_external_thread_id")
        ),
    )
    op.create_index("ix_threads_investor_id", "threads", ["investor_id"], unique=False)
    op.create_index("ix_threads_seller_lead_id", "threads", ["seller_lead_id"], unique=False)

    op.create_table(
        "sequences",
        sa.Column(
            "kind",
            sa.Enum(
                "investor",
                "seller",
                name="sequence_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("investor_id", GUID(), nullable=True),
        sa.Column("seller_lead_id", GUID(), nullable=True),
        sa.Column("thread_id", GUID(), nullable=True),
        sa.Column("temporal_workflow_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "paused",
                "completed",
                "cancelled",
                name="sequence_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("total_steps", sa.Integer(), nullable=False),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=255), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "(kind = 'investor' AND investor_id IS NOT NULL AND seller_lead_id IS NULL) OR (kind = 'seller' AND seller_lead_id IS NOT NULL AND investor_id IS NULL)",
            name=op.f("ck_sequences_target_matches_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["investor_id"],
            ["investors.id"],
            name=op.f("fk_sequences_investor_id_investors"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["seller_lead_id"],
            ["seller_leads.id"],
            name=op.f("fk_sequences_seller_lead_id_seller_leads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
            name=op.f("fk_sequences_thread_id_threads"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sequences")),
        sa.UniqueConstraint("temporal_workflow_id", name=op.f("uq_sequences_temporal_workflow_id")),
    )
    op.create_index("ix_sequences_investor_id", "sequences", ["investor_id"], unique=False)
    op.create_index("ix_sequences_seller_lead_id", "sequences", ["seller_lead_id"], unique=False)
    op.create_index("ix_sequences_status", "sequences", ["status"], unique=False)

    op.create_table(
        "messages",
        sa.Column("thread_id", GUID(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("in", "out", name="direction", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column(
            "channel",
            sa.Enum(
                "email",
                "sms",
                "web_form",
                name="channel",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("external_message_id", sa.String(length=512), nullable=True),
        sa.Column("in_reply_to_external_id", sa.String(length=512), nullable=True),
        sa.Column("from_address", sa.String(length=512), nullable=True),
        sa.Column("to_addresses", JSONDoc(), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column(
            "classification",
            sa.Enum(
                "interested",
                "buy_box_supplied",
                "question",
                "not_now",
                "not_interested",
                "opt_out",
                "ambiguous",
                "unrelated",
                name="message_classification",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("classification_confidence", sa.Double(), nullable=True),
        sa.Column("draft", sa.Boolean(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("policy_action", sa.String(length=64), nullable=True),
        sa.Column(
            "policy_decision",
            sa.Enum(
                "send",
                "queue_for_human",
                "blocked",
                name="policy_decision",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("qa_review_id", GUID(), nullable=True),
        sa.Column("sequence_id", GUID(), nullable=True),
        sa.Column("sequence_step", sa.Integer(), nullable=True),
        sa.Column("agent_run_id", GUID(), nullable=True),
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
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_messages_agent_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sequence_id"],
            ["sequences.id"],
            name=op.f("fk_messages_sequence_id_sequences"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
            name=op.f("fk_messages_thread_id_threads"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "channel", "external_message_id", name=op.f("uq_messages_channel_external_message_id")
        ),
    )
    op.create_index("ix_messages_classification", "messages", ["classification"], unique=False)
    op.create_index("ix_messages_direction_draft", "messages", ["direction", "draft"], unique=False)
    op.create_index("ix_messages_sequence_id", "messages", ["sequence_id"], unique=False)
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"], unique=False)

    op.create_table(
        "buy_boxes",
        sa.Column("investor_id", GUID(), nullable=False),
        sa.Column("buy_box_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("geography", JSONDoc(), nullable=False),
        sa.Column("asset_types", JSONDoc(), nullable=False),
        sa.Column("price_min", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("price_max", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("condition", sa.String(length=64), nullable=True),
        sa.Column("strategy", sa.String(length=64), nullable=True),
        sa.Column("financing", sa.String(length=64), nullable=True),
        sa.Column("closing_preferences", JSONDoc(), nullable=False),
        sa.Column("other_criteria", JSONDoc(), nullable=False),
        sa.Column("unknown_fields", JSONDoc(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("source_message_id", GUID(), nullable=True),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("change_note", sa.String(length=255), nullable=True),
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
            ["investor_id"],
            ["investors.id"],
            name=op.f("fk_buy_boxes_investor_id_investors"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            name=op.f("fk_buy_boxes_source_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_buy_boxes")),
        sa.UniqueConstraint(
            "investor_id",
            "buy_box_key",
            "version",
            name=op.f("uq_buy_boxes_investor_id_buy_box_key_version"),
        ),
    )
    op.create_index(
        "ix_buy_boxes_investor_current", "buy_boxes", ["investor_id", "is_current"], unique=False
    )
    op.create_index("ix_buy_boxes_investor_id", "buy_boxes", ["investor_id"], unique=False)
    op.create_index(
        "ix_buy_boxes_source_message_id", "buy_boxes", ["source_message_id"], unique=False
    )

    op.create_table(
        "offers",
        sa.Column("seller_lead_id", GUID(), nullable=False),
        sa.Column("intake_id", GUID(), nullable=False),
        sa.Column("parcel_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "pending_review",
                "blocked",
                "sent",
                "countered",
                "accepted",
                "declined",
                "expired",
                "withdrawn",
                name="offer_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("arv", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "arv_confidence",
            sa.Enum(
                "High",
                "Medium",
                "Low",
                name="arv_confidence",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("arv_avm", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("comp_tier", sa.Integer(), nullable=True),
        sa.Column("repairs", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("market_pct", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("assignment_fee", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("closing_costs", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("mao", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("negotiation_room_pct", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("offer_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("gates", JSONDoc(), nullable=False),
        sa.Column("open_flags", JSONDoc(), nullable=False),
        sa.Column("calc_record", JSONDoc(), nullable=False),
        sa.Column("draft_body", sa.Text(), nullable=True),
        sa.Column(
            "policy_level_at_send",
            sa.Enum(
                "L0",
                "L1",
                "L2",
                name="autonomy_level",
                native_enum=False,
                create_constraint=True,
                length=4,
            ),
            nullable=True,
        ),
        sa.Column("sent_message_id", GUID(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
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
            ["intake_id"],
            ["intakes.id"],
            name=op.f("fk_offers_intake_id_intakes"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["seller_lead_id"],
            ["seller_leads.id"],
            name=op.f("fk_offers_seller_lead_id_seller_leads"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sent_message_id"],
            ["messages.id"],
            name=op.f("fk_offers_sent_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offers")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_offers_idempotency_key")),
    )
    op.create_index("ix_offers_intake_id", "offers", ["intake_id"], unique=False)
    op.create_index("ix_offers_parcel_id", "offers", ["parcel_id"], unique=False)
    op.create_index("ix_offers_seller_lead_id", "offers", ["seller_lead_id"], unique=False)
    op.create_index("ix_offers_status", "offers", ["status"], unique=False)

    op.create_table(
        "suppression",
        sa.Column(
            "channel",
            sa.Enum(
                "email",
                "sms",
                "web_form",
                name="channel",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("normalized_value", sa.String(length=512), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("source_message_id", GUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("id", GUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            name=op.f("fk_suppression_source_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppression")),
        sa.UniqueConstraint(
            "channel", "normalized_value", name=op.f("uq_suppression_channel_normalized_value")
        ),
    )
    op.create_table(
        "offer_verifications",
        sa.Column("offer_id", GUID(), nullable=False),
        sa.Column(
            "pass_id",
            sa.Enum(
                "V1",
                "V2",
                "V3",
                "V4",
                "V5",
                "V6",
                name="verification_pass",
                native_enum=False,
                create_constraint=True,
                length=4,
            ),
            nullable=False,
        ),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("inputs", JSONDoc(), nullable=False),
        sa.Column("outputs", JSONDoc(), nullable=False),
        sa.Column(
            "result",
            sa.Enum(
                "pass",
                "fail",
                "adjusted",
                "escalate",
                name="verification_result",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("agent_run_id", GUID(), nullable=True),
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
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_offer_verifications_agent_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_offer_verifications_offer_id_offers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_verifications")),
        sa.UniqueConstraint(
            "offer_id",
            "pass_id",
            "iteration",
            name=op.f("uq_offer_verifications_offer_id_pass_id_iteration"),
        ),
    )
    op.create_index(
        "ix_offer_verifications_offer_id", "offer_verifications", ["offer_id"], unique=False
    )

    op.create_table(
        "qa_reviews",
        sa.Column("message_id", GUID(), nullable=True),
        sa.Column("offer_id", GUID(), nullable=True),
        sa.Column(
            "verdict",
            sa.Enum(
                "approve",
                "block",
                "rewrite",
                name="qa_verdict",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("confidence", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("flags", JSONDoc(), nullable=False),
        sa.Column("rewrite_body", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("agent_run_id", GUID(), nullable=True),
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
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_qa_reviews_agent_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_qa_reviews_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["offer_id"],
            ["offers.id"],
            name=op.f("fk_qa_reviews_offer_id_offers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qa_reviews")),
    )
    op.create_index("ix_qa_reviews_message_id", "qa_reviews", ["message_id"], unique=False)
    op.create_index("ix_qa_reviews_offer_id", "qa_reviews", ["offer_id"], unique=False)
    op.create_index("ix_qa_reviews_verdict", "qa_reviews", ["verdict"], unique=False)

    op.create_table(
        "search_profiles",
        sa.Column("buy_box_id", GUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("criteria", JSONDoc(), nullable=False),
        sa.Column("unknowns", JSONDoc(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "superseded",
                "inactive",
                name="search_profile_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("criteria_hash", sa.String(length=64), nullable=True),
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
            ["buy_box_id"],
            ["buy_boxes.id"],
            name=op.f("fk_search_profiles_buy_box_id_buy_boxes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_profiles")),
        sa.UniqueConstraint(
            "buy_box_id", "version", name=op.f("uq_search_profiles_buy_box_id_version")
        ),
    )
    op.create_index(
        "ix_search_profiles_buy_box_id", "search_profiles", ["buy_box_id"], unique=False
    )
    op.create_index("ix_search_profiles_status", "search_profiles", ["status"], unique=False)

    op.create_table(
        "matches",
        sa.Column("search_profile_id", GUID(), nullable=False),
        sa.Column("property_candidate_id", GUID(), nullable=False),
        sa.Column("score", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("evidence", JSONDoc(), nullable=False),
        sa.Column(
            "review_status",
            sa.Enum(
                "pending",
                "approved",
                "rejected",
                name="match_review_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("reviewed_by", sa.String(length=120), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_run_id", GUID(), nullable=True),
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
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_matches_agent_run_id_agent_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_candidate_id"],
            ["property_candidates.id"],
            name=op.f("fk_matches_property_candidate_id_property_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["search_profile_id"],
            ["search_profiles.id"],
            name=op.f("fk_matches_search_profile_id_search_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_matches")),
        sa.UniqueConstraint(
            "search_profile_id",
            "property_candidate_id",
            name=op.f("uq_matches_search_profile_id_property_candidate_id"),
        ),
    )
    op.create_index(
        "ix_matches_property_candidate_id", "matches", ["property_candidate_id"], unique=False
    )
    op.create_index("ix_matches_review_status", "matches", ["review_status"], unique=False)
    op.create_index("ix_matches_search_profile_id", "matches", ["search_profile_id"], unique=False)

    _seed_policies()


def _policies_table() -> sa.TableClause:
    return sa.table(
        "policies",
        sa.column("id", GUID()),
        sa.column("action", sa.String(40)),
        sa.column("level", sa.String(4)),
        sa.column("daily_cap", sa.Integer()),
        sa.column("threshold", sa.Numeric(6, 4)),
        sa.column("updated_by", sa.String(120)),
        sa.column("note", sa.String(255)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _seed_policies() -> None:
    """POL-02: every one of the 12 v2 actions starts at L0 (``default_level()``)."""
    now = utcnow()
    level = default_level().value
    op.bulk_insert(
        _policies_table(),
        [
            {
                "id": uuid.uuid4(),
                "action": action.value,
                "level": level,
                "daily_cap": None,
                "threshold": None,
                "updated_by": "migration:0001",
                "note": "seeded at L0 (POL-02)",
                "created_at": now,
                "updated_at": now,
            }
            for action in PolicyAction
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_matches_search_profile_id", table_name="matches")
    op.drop_index("ix_matches_review_status", table_name="matches")
    op.drop_index("ix_matches_property_candidate_id", table_name="matches")

    op.drop_table("matches")
    op.drop_index("ix_search_profiles_status", table_name="search_profiles")
    op.drop_index("ix_search_profiles_buy_box_id", table_name="search_profiles")

    op.drop_table("search_profiles")
    op.drop_index("ix_qa_reviews_verdict", table_name="qa_reviews")
    op.drop_index("ix_qa_reviews_offer_id", table_name="qa_reviews")
    op.drop_index("ix_qa_reviews_message_id", table_name="qa_reviews")

    op.drop_table("qa_reviews")
    op.drop_index("ix_offer_verifications_offer_id", table_name="offer_verifications")

    op.drop_table("offer_verifications")
    op.drop_table("suppression")
    op.drop_index("ix_offers_status", table_name="offers")
    op.drop_index("ix_offers_seller_lead_id", table_name="offers")
    op.drop_index("ix_offers_parcel_id", table_name="offers")
    op.drop_index("ix_offers_intake_id", table_name="offers")

    op.drop_table("offers")
    op.drop_index("ix_buy_boxes_source_message_id", table_name="buy_boxes")
    op.drop_index("ix_buy_boxes_investor_id", table_name="buy_boxes")
    op.drop_index("ix_buy_boxes_investor_current", table_name="buy_boxes")

    op.drop_table("buy_boxes")
    op.drop_index("ix_messages_thread_id", table_name="messages")
    op.drop_index("ix_messages_sequence_id", table_name="messages")
    op.drop_index("ix_messages_direction_draft", table_name="messages")
    op.drop_index("ix_messages_classification", table_name="messages")

    op.drop_table("messages")
    op.drop_index("ix_sequences_status", table_name="sequences")
    op.drop_index("ix_sequences_seller_lead_id", table_name="sequences")
    op.drop_index("ix_sequences_investor_id", table_name="sequences")

    op.drop_table("sequences")
    op.drop_index("ix_threads_seller_lead_id", table_name="threads")
    op.drop_index("ix_threads_investor_id", table_name="threads")

    op.drop_table("threads")
    op.drop_index("ix_source_evidence_source_ref", table_name="source_evidence")
    op.drop_index("ix_source_evidence_seller_lead_id", table_name="source_evidence")
    op.drop_index("ix_source_evidence_investor_id", table_name="source_evidence")

    op.drop_table("source_evidence")
    op.drop_index("ix_property_candidates_parcel_id", table_name="property_candidates")
    op.drop_index("ix_property_candidates_market_id", table_name="property_candidates")
    op.drop_index("ix_property_candidates_address_key", table_name="property_candidates")

    op.drop_table("property_candidates")
    op.drop_index("ix_intakes_seller_lead_id", table_name="intakes")
    op.drop_index("ix_intakes_intake_complete", table_name="intakes")

    op.drop_table("intakes")
    op.drop_index("ix_contacts_investor_id", table_name="contacts")

    op.drop_table("contacts")
    op.drop_index("ix_seller_leads_status", table_name="seller_leads")
    op.drop_index("ix_seller_leads_parcel_id", table_name="seller_leads")
    op.drop_index("ix_seller_leads_market_id", table_name="seller_leads")
    op.drop_index("ix_seller_leads_address_key", table_name="seller_leads")

    op.drop_table("seller_leads")
    op.drop_index("ix_investors_score", table_name="investors")
    op.drop_index("ix_investors_market_id", table_name="investors")
    op.drop_index("ix_investors_investor_type", table_name="investors")

    op.drop_table("investors")
    op.drop_table("policies")
    op.drop_index("ix_markets_tier", table_name="markets")

    op.drop_table("markets")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")

    op.drop_table("audit_events")
    op.drop_index("ix_agent_runs_correlation_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent", table_name="agent_runs")

    op.drop_table("agent_runs")
