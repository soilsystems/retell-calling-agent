"""delivery status on whatsapp_messages

Adds `status` (sent/delivered/read/failed) and `status_detail` (Exotel's
exo_detailed_status / description) so the chat UI can show whether an outbound
message was actually delivered instead of always implying it was sent. Updated
from Exotel delivery-receipt (DLR) callbacks, matched by provider_message_id.

Both columns are nullable with no default, so this ALTER is metadata-only
(instant, lock-light) on the Supabase pooler.

Revision ID: 010_whatsapp_message_status
Revises: 009_leads_site_visit
Create Date: 2026-06-29 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010_whatsapp_message_status"
down_revision: str | None = "009_leads_site_visit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("whatsapp_messages", sa.Column("status", sa.Text(), nullable=True))
    op.add_column("whatsapp_messages", sa.Column("status_detail", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("whatsapp_messages", "status_detail")
    op.drop_column("whatsapp_messages", "status")
