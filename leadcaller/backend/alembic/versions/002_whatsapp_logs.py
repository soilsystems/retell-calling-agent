"""whatsapp logs

Revision ID: 002_whatsapp_logs
Revises: 001_initial
Create Date: 2026-05-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_whatsapp_logs"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    whatsapp_log_status = postgresql.ENUM("sent", "failed", "skipped", name="whatsapp_log_status")
    whatsapp_log_status.create(bind, checkfirst=True)

    op.create_table(
        "whatsapp_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "call_attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("call_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phone", sa.Text()),
        sa.Column("template_name", sa.Text()),
        sa.Column(
            "status",
            postgresql.ENUM("sent", "failed", "skipped", name="whatsapp_log_status", create_type=False),
            nullable=False,
        ),
        sa.Column("wati_response", postgresql.JSONB()),
        sa.Column("error_message", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
    )
    op.execute("ALTER TABLE whatsapp_logs ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY service_role_all_whatsapp_logs ON whatsapp_logs FOR ALL USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS service_role_all_whatsapp_logs ON whatsapp_logs")
    op.drop_table("whatsapp_logs")
    op.execute("DROP TYPE IF EXISTS whatsapp_log_status")
