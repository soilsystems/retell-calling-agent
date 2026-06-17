import json
# pyrefly: ignore [missing-import]
import pytest
from httpx import AsyncClient
from app.database import get_db
from app.main import app

WEBHOOK_VERIFY_TOKEN = "leadcaller_webhook_2024"


class EmptyResult:
    def scalar_one_or_none(self):
        return None


class FakeDb:
    def __init__(self):
        self.added = []
        self.committed = False

    async def execute(self, stmt):
        return EmptyResult()

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.committed = True

    async def refresh(self, item):
        pass


@pytest.fixture(name="fake_db")
def override_db_fixture():
    db_instance = FakeDb()
    async def fake_get_db():
        yield db_instance
    app.dependency_overrides[get_db] = fake_get_db
    yield db_instance
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_whatsapp_health(client: AsyncClient):
    response = await client.get("/whatsapp/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "whatsapp"}


@pytest.mark.asyncio
async def test_whatsapp_webhook_valid_token_and_payload(client: AsyncClient, fake_db):
    payload = {
        "verify_token": WEBHOOK_VERIFY_TOKEN,
        "from": "+919876543210",
        "body": "Hello, this is a test message!",
        "type": "text",
        "timestamp": 1672531199,
        "message_id": "ABEGxxxxxxxxxxxx",
    }
    response = await client.post("/whatsapp/webhook", json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "message": "WhatsApp message received",
        "message_id": "ABEGxxxxxxxxxxxx",
    }
    
    # Verify both the webhook event AND the chat message were added
    assert len(fake_db.added) == 2
    event = next(x for x in fake_db.added if hasattr(x, "idempotency_key"))
    assert event.source == "whatsapp"
    assert event.event_type == "incoming_message"
    assert event.payload == payload
    assert event.idempotency_key == "ABEGxxxxxxxxxxxx"
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_whatsapp_webhook_valid_token_query_params(client: AsyncClient, fake_db):
    payload = {
        "from": "+919876543210",
        "body": "Hello, this is a test message!",
        "type": "text",
        "timestamp": 1672531199,
        "message_id": "ABEGxxxxxxxxxxxx",
    }
    response = await client.post(
        "/whatsapp/webhook",
        json=payload,
        params={"verify_token": WEBHOOK_VERIFY_TOKEN},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "message": "WhatsApp message received",
        "message_id": "ABEGxxxxxxxxxxxx",
    }
    assert len(fake_db.added) == 2
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_whatsapp_webhook_invalid_token(client: AsyncClient):
    payload = {
        "verify_token": "wrong_token",
        "from": "+919876543210",
        "body": "Hello",
        "type": "text",
        "timestamp": 1672531199,
        "message_id": "ABEGxxxxxxxxxxxx",
    }
    response = await client.post("/whatsapp/webhook", json=payload)
    assert response.status_code == 401
    assert "invalid verify token" in response.json()["detail"]


@pytest.mark.asyncio
async def test_whatsapp_webhook_exotel_no_token_accepted(client: AsyncClient, fake_db):
    """Exotel WhatsApp webhooks don't send a verify_token — accept payloads that omit it."""
    payload = {
        "from": "+919876543210",
        "body": "Hi from Exotel",
        "type": "text",
        "timestamp": 1672531199,
        "message_id": "EXOTEL_NO_TOKEN_001",
    }
    response = await client.post("/whatsapp/webhook", json=payload)
    assert response.status_code == 200
    assert response.json()["message_id"] == "EXOTEL_NO_TOKEN_001"
    assert len(fake_db.added) == 2


@pytest.mark.asyncio
async def test_whatsapp_webhook_invalid_json(client: AsyncClient):
    response = await client.post(
        "/whatsapp/webhook",
        content="invalid json content",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "invalid JSON payload" in response.json()["detail"]


@pytest.mark.asyncio
async def test_whatsapp_status_webhook_valid_token(client: AsyncClient, fake_db):
    payload = {
        "verify_token": WEBHOOK_VERIFY_TOKEN,
        "from": "+919876543210",
        "type": "text",
        "timestamp": 1672531199,
        "message_id": "ABEGxxxxxxxxxxxx",
        "status": "delivered",
    }
    response = await client.post("/whatsapp/webhook/status", json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "message": "WhatsApp status callback received",
        "message_id": "ABEGxxxxxxxxxxxx",
    }
    
    # Verify the status event was added and committed to the fake DB
    assert len(fake_db.added) == 1
    event = fake_db.added[0]
    assert event.source == "whatsapp"
    assert event.event_type == "status_delivered"
    assert event.payload == payload
    assert event.idempotency_key == "status:ABEGxxxxxxxxxxxx:delivered"
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_whatsapp_status_webhook_invalid_token(client: AsyncClient):
    payload = {
        "verify_token": "wrong_token",
        "from": "+919876543210",
        "type": "text",
        "timestamp": 1672531199,
        "message_id": "ABEGxxxxxxxxxxxx",
        "status": "delivered",
    }
    response = await client.post("/whatsapp/webhook/status", json=payload)
    assert response.status_code == 401
    assert "invalid verify token" in response.json()["detail"]


@pytest.mark.asyncio
async def test_whatsapp_status_webhook_invalid_json(client: AsyncClient):
    response = await client.post(
        "/whatsapp/webhook/status",
        content="invalid json content",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "invalid JSON payload" in response.json()["detail"]
