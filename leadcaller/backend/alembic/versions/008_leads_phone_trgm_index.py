"""leads.phone trigram index for fast suffix matching

Phone lookups use LIKE '%<10-digit-suffix>' to tolerate +91/91/0/no-prefix
variations. With a leading wildcard B-tree indexes are useless, forcing a
full table scan that hits the Postgres statement_timeout on large lead tables.
A pg_trgm GIN index makes substring LIKE fast (microseconds).

Revision ID: 008_leads_phone_trgm_index
Revises: 007_whatsapp_messages
Create Date: 2026-06-19 00:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "008_leads_phone_trgm_index"
down_revision: str | None = "007_whatsapp_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Run outside the migration's wrapping transaction so we can:
    #   1. SET statement_timeout = 0     (Supabase's default kills slow DDL)
    #   2. CREATE INDEX CONCURRENTLY     (forbidden inside a transaction)
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("SET statement_timeout = 0")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_leads_phone_trgm "
            "ON leads USING gin (phone gin_trgm_ops)"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_leads_phone_trgm")
