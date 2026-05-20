import json
from datetime import datetime

import pytest


def lead_payload(**overrides):
    payload = {
        "zoho_lead_id": "5900000000001",
        "name": "Asha Rao",
        "phone": "+919876543210",
        "email": "asha@example.com",
        "city": "Bengaluru",
        "language_preference": "english",
        "source": "Zoho",
        "campaign": "May Homes",
        "received_at": "2026-05-14T10:00:00+05:30",
    }
    payload.update(overrides)
    return payload


class EmptyResult:
    def scalar_one_or_none(self):
        return None


class FakeWebhookDb:
    async def execute(self, stmt):
        return EmptyResult()

    def add(self, item):
        return None

    async def commit(self):
        return None

    async def refresh(self, item):
        return None


async def fake_get_empty_db():
    yield FakeWebhookDb()


@pytest.mark.asyncio
async def test_valid_signature_accepted(client, zoho_signature, monkeypatch):
    async def fake_schedule(payload, webhook_event, db, now=None):
        class Job:
            id = "11111111-1111-1111-1111-111111111111"
            scheduled_at = datetime.fromisoformat("2026-05-14T04:30:00+00:00")

        return "scheduled", Job()

    monkeypatch.setattr("app.routers.webhooks.schedule_call_for_lead", fake_schedule)
    monkeypatch.setattr("app.routers.webhooks.trigger_retell_call", lambda call_job_id: None)
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = fake_get_empty_db
    body = json.dumps(lead_payload()).encode()
    response = await client.post("/webhooks/zoho/new-lead", content=body, headers={"X-Zoho-Webhook-Token": zoho_signature(body)})
    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["status"] == "scheduled"


@pytest.mark.asyncio
async def test_invalid_signature_rejected_401(client):
    body = json.dumps(lead_payload()).encode()
    response = await client.post("/webhooks/zoho/new-lead", content=body, headers={"X-Zoho-Webhook-Token": "bad"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_duplicate_webhook_returns_200_without_processing(client, zoho_signature, monkeypatch):
    class ExistingEvent:
        processed = True

    class Result:
        def scalar_one_or_none(self):
            return ExistingEvent()

    class Db:
        async def execute(self, stmt):
            return Result()

    async def fake_get_db():
        yield Db()

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = fake_get_db
    body = json.dumps(lead_payload()).encode()
    response = await client.post("/webhooks/zoho/new-lead", content=body, headers={"X-Zoho-Webhook-Token": zoho_signature(body)})
    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["status"] == "already handled"


@pytest.mark.asyncio
async def test_phone_validation_rejects_landline(client, zoho_signature):
    body = json.dumps(lead_payload(phone="+911122334455")).encode()
    response = await client.post("/webhooks/zoho/new-lead", content=body, headers={"X-Zoho-Webhook-Token": zoho_signature(body)})
    assert response.status_code == 422


def test_outside_business_hours_schedules_next_slot():
    from app.utils.business_hours import next_business_slot
    from zoneinfo import ZoneInfo

    dt = datetime(2026, 5, 17, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    slot = next_business_slot(dt).astimezone(ZoneInfo("Asia/Kolkata"))
    assert slot.weekday() == 0
    assert slot.hour == 9


@pytest.mark.asyncio
async def test_duplicate_call_job_not_created(client, zoho_signature, monkeypatch):
    async def fake_schedule(payload, webhook_event, db, now=None):
        return "call already scheduled", object()

    monkeypatch.setattr("app.routers.webhooks.schedule_call_for_lead", fake_schedule)
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = fake_get_empty_db
    body = json.dumps(lead_payload()).encode()
    response = await client.post("/webhooks/zoho/new-lead", content=body, headers={"X-Zoho-Webhook-Token": zoho_signature(body)})
    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["status"] == "call already scheduled"
