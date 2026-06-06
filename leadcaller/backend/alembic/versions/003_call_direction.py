"""call direction

Revision ID: 003_call_direction
Revises: 002_whatsapp_logs
Create Date: 2026-06-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003_call_direction"
down_revision: str | None = "002_whatsapp_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    call_direction = postgresql.ENUM("inbound", "outbound", name="call_direction")
    call_direction.create(bind, checkfirst=True)
    op.add_column(
        "call_attempts",
        sa.Column(
            "direction",
            postgresql.ENUM("inbound", "outbound", name="call_direction", create_type=False),
            nullable=False,
            server_default="outbound",
        ),
    )


def downgrade() -> None:
    op.drop_column("call_attempts", "direction")
    op.execute("DROP TYPE IF EXISTS call_direction")
