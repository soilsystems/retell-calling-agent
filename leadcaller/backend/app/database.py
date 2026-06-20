from collections.abc import AsyncGenerator

import uuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models."""


settings = get_settings()

# Opening a fresh asyncpg connection to Supabase's cross-region pooler costs
# ~2.5s of handshake. The old NullPool paid that on EVERY request, so the Retell
# inbound webhook took >2s and calls struggled to connect. A reused connection
# runs queries in ~150ms, so we pool connections instead.
#
# Pooling requires the SESSION pooler (port 5432): it gives each client
# connection a dedicated server backend, so asyncpg prepared statements work.
# (The transaction pooler on 6543 multiplexes backends per-transaction, which
# breaks prepared statements when a pooled connection is reused — that's why the
# old setup needed NullPool.) We rewrite :6543 -> :5432 for the app engine.
_db_url = str(settings.DATABASE_URL).replace(":6543/", ":5432/")

engine = create_async_engine(
    _db_url,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=180,
    pool_timeout=10,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    }
)
# NOTE: a leaked "idle in transaction" backend isn't capped at the connection
# level, so a scheduled sweeper (sweep_idle_db_connections) terminates them.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
