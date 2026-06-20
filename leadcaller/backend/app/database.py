from collections.abc import AsyncGenerator

import uuid
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models."""


settings = get_settings()
engine = create_async_engine(
    str(settings.DATABASE_URL),
    poolclass=NullPool,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    }
)
# NOTE: Supabase's transaction pooler (port 6543) ignores session GUCs like
# idle_in_transaction_session_timeout set via server_settings, so leaked
# "idle in transaction" backends can't be capped at the connection level.
# Instead a scheduled sweeper (sweep_idle_db_connections) terminates them.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
