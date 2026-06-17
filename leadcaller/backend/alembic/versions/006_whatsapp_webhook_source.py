"""whatsapp webhook source

Revision ID: 006_whatsapp_webhook_source
Revises: 005_meta_webhook_source
Create Date: 2026-06-16 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "006_whatsapp_webhook_source"
down_revision: str | None = "005_meta_webhook_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE webhook_source ADD VALUE IF NOT EXISTS 'whatsapp'")


def downgrade() -> None:
    # PostgreSQL cannot drop enum values safely without recreating the type.
    pass
