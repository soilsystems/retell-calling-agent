"""scheduler fields

Revision ID: 004_scheduler_fields
Revises: 003_call_direction
Create Date: 2026-06-05 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "004_scheduler_fields"
down_revision: str | None = "003_call_direction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE call_jobs ADD COLUMN IF NOT EXISTS trigger_reason VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE call_jobs DROP COLUMN IF EXISTS trigger_reason")
