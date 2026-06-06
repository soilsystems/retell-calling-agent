"""meta webhook source

Revision ID: 005_meta_webhook_source
Revises: 004_scheduler_fields
Create Date: 2026-06-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "005_meta_webhook_source"
down_revision: str | None = "004_scheduler_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE webhook_source ADD VALUE IF NOT EXISTS 'meta'")


def downgrade() -> None:
    # PostgreSQL cannot drop enum values safely without recreating the type.
    pass
