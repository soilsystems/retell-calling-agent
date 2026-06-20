"""site-visit + manual visited tracking on leads

Adds columns to record whether a site visit was fixed (mirrored from the
latest call's structured data) and a manual "visited" flag set from the
dashboard, which triggers a one-time feedback WhatsApp.

Revision ID: 009_leads_site_visit
Revises: 008_leads_phone_trgm_index
Create Date: 2026-06-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "009_leads_site_visit"
down_revision: str | None = "008_leads_phone_trgm_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("site_visit_fixed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("leads", sa.Column("site_visit_date", sa.String(length=120), nullable=True))
    op.add_column(
        "leads",
        sa.Column("visited", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("leads", sa.Column("visited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "leads",
        sa.Column("feedback_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("leads", "feedback_sent")
    op.drop_column("leads", "visited_at")
    op.drop_column("leads", "visited")
    op.drop_column("leads", "site_visit_date")
    op.drop_column("leads", "site_visit_fixed")
