"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-05-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    language_preference = postgresql.ENUM("hindi", "english", "kannada", name="language_preference")
    call_job_status = postgresql.ENUM("pending", "in_progress", "completed", "failed", "cancelled", name="call_job_status")
    call_attempt_status = postgresql.ENUM(
        "initiated", "ringing", "answered", "no_answer", "busy", "failed", "completed", name="call_attempt_status"
    )
    webhook_source = postgresql.ENUM("zoho", "retell", name="webhook_source")
    followup_status = postgresql.ENUM("pending", "created", "failed", name="followup_status")
    bind = op.get_bind()
    language_preference.create(bind, checkfirst=True)
    call_job_status.create(bind, checkfirst=True)
    call_attempt_status.create(bind, checkfirst=True)
    webhook_source.create(bind, checkfirst=True)
    followup_status.create(bind, checkfirst=True)

    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("zoho_lead_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(length=320)),
        sa.Column("city", sa.String(length=120)),
        sa.Column("language_preference", language_preference, nullable=False, server_default="english"),
        sa.Column("source", sa.String(length=120)),
        sa.Column("campaign", sa.String(length=120)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
    )
    op.create_index("ix_leads_phone", "leads", ["phone"])
    op.create_index("ix_leads_zoho_lead_id", "leads", ["zoho_lead_id"])

    op.create_table(
        "call_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", call_job_status, nullable=False, server_default="pending"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
    )

    op.create_table(
        "call_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("call_job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("call_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("retell_call_id", sa.Text(), nullable=False, unique=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", call_attempt_status, nullable=False, server_default="initiated"),
        sa.Column("recording_url", sa.Text()),
        sa.Column("transcript", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("structured_data", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Integer()),
    )
    op.create_index("ix_call_attempts_retell_call_id", "call_attempts", ["retell_call_id"])

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source", webhook_source, nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
    )
    op.create_index("ix_webhook_events_idempotency_key", "webhook_events", ["idempotency_key"])

    op.create_table(
        "crm_sync_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="SET NULL")),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
    )

    op.create_table(
        "followups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_attempt_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("call_attempts.id", ondelete="SET NULL")),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("zoho_task_id", sa.Text()),
        sa.Column("status", followup_status, nullable=False, server_default="pending"),
    )

    op.create_table(
        "campaign_metrics",
        sa.Column("campaign", sa.Text(), primary_key=True),
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("total_leads", sa.Integer(), nullable=False),
        sa.Column("calls_made", sa.Integer(), nullable=False),
        sa.Column("answered", sa.Integer(), nullable=False),
        sa.Column("hot_leads", sa.Integer(), nullable=False),
        sa.Column("conversion_rate", sa.Float(), nullable=False),
    )

    op.create_table(
        "zoho_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
    )

    for table in [
        "leads",
        "call_jobs",
        "call_attempts",
        "webhook_events",
        "crm_sync_logs",
        "followups",
        "campaign_metrics",
        "zoho_tokens",
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY service_role_all_{table} ON {table} FOR ALL USING (true) WITH CHECK (true)")

    op.execute(
        """
        CREATE MATERIALIZED VIEW IF NOT EXISTS campaign_metrics_nightly AS
        SELECT
            l.campaign,
            date_trunc('day', l.created_at)::date AS date,
            count(DISTINCT l.id)::integer AS total_leads,
            count(DISTINCT ca.id)::integer AS calls_made,
            count(DISTINCT ca.id) FILTER (WHERE ca.status IN ('answered', 'completed'))::integer AS answered,
            count(DISTINCT ca.id) FILTER (WHERE ca.structured_data->>'interest_level' = 'Hot')::integer AS hot_leads,
            COALESCE(
                (count(DISTINCT ca.id) FILTER (WHERE ca.structured_data->>'interest_level' = 'Hot')::float
                 / NULLIF(count(DISTINCT l.id), 0)),
                0
            ) AS conversion_rate
        FROM leads l
        LEFT JOIN call_jobs cj ON cj.lead_id = l.id
        LEFT JOIN call_attempts ca ON ca.call_job_id = cj.id
        GROUP BY l.campaign, date_trunc('day', l.created_at)::date
        """
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS campaign_metrics_nightly")
    for table in [
        "zoho_tokens",
        "campaign_metrics",
        "followups",
        "crm_sync_logs",
        "webhook_events",
        "call_attempts",
        "call_jobs",
        "leads",
    ]:
        op.drop_table(table)
    for enum_name in [
        "followup_status",
        "webhook_source",
        "call_attempt_status",
        "call_job_status",
        "language_preference",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
