import hashlib
import hmac
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "service-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/leadcaller_test")
os.environ.setdefault("ZOHO_CLIENT_ID", "zoho-client")
os.environ.setdefault("ZOHO_CLIENT_SECRET", "zoho-secret")
os.environ.setdefault("ZOHO_REDIRECT_URI", "https://example.com/oauth")
os.environ.setdefault("ZOHO_WEBHOOK_SECRET", "zoho-webhook-secret")
os.environ.setdefault("ZOHO_REFRESH_TOKEN", "zoho-refresh-token")
os.environ.setdefault("RETELL_API_KEY", "retell-key")
os.environ.setdefault("RETELL_AGENT_ID", "agent-id")
os.environ.setdefault("RETELL_FROM_NUMBER", "+911234567890")
os.environ.setdefault("RETELL_WEBHOOK_SECRET", "retell-webhook-secret")
os.environ.setdefault("BASE_URL", "https://leadcaller.example.com")
os.environ.setdefault("ENVIRONMENT", "dev")

from app.main import app  # noqa: E402


def sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@pytest.fixture
def zoho_signature():
    return lambda payload: sign(payload, os.environ["ZOHO_WEBHOOK_SECRET"])


@pytest.fixture
def retell_signature():
    return lambda payload: sign(payload, os.environ["RETELL_WEBHOOK_SECRET"])


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def session_factory():
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
