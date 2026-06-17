"""whatsapp messages table for two-way chat UI

Revision ID: 007_whatsapp_messages
Revises: 006_whatsapp_webhook_source
Create Date: 2026-06-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007_whatsapp_messages"
down_revision: str | None = "006_whatsapp_webhook_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    direction_enum = postgresql.ENUM(
        "inbound", "outbound", name="whatsapp_message_direction", create_type=False
    )
    type_enum = postgresql.ENUM(
        "text", "image", "document", "video", "audio", "location", "template", "other",
        name="whatsapp_message_type", create_type=False,
    )
    direction_enum.create(op.get_bind(), checkfirst=True)
    type_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "whatsapp_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("direction", direction_enum, nullable=False),
        sa.Column("message_type", type_enum, nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media_url", sa.Text(), nullable=True),
        sa.Column("media_filename", sa.Text(), nullable=True),
        sa.Column("media_caption", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Text(), nullable=True),
        sa.Column("longitude", sa.Text(), nullable=True),
        sa.Column("location_name", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True, unique=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_whatsapp_messages_phone", "whatsapp_messages", ["phone"])
    op.create_index("ix_whatsapp_messages_phone_created_at", "whatsapp_messages", ["phone", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_whatsapp_messages_phone_created_at", table_name="whatsapp_messages")
    op.drop_index("ix_whatsapp_messages_phone", table_name="whatsapp_messages")
    op.drop_table("whatsapp_messages")
    op.execute("DROP TYPE IF EXISTS whatsapp_message_type")
    op.execute("DROP TYPE IF EXISTS whatsapp_message_direction")
