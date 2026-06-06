from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models import CallAttempt, CallDirection, CallJob, CallJobStatus, LanguagePreference, Lead
from app.schemas.retell_schema import RetellCallCompletedWebhook
from app.services import retell_service


def inbound_payload(**overrides):
    payload = {
        "call_id": "inbound-call-1",
        "call_status": "completed",
        "direction": "inbound",
        "from_number": "+919876543210",
        "to_number": "+911234567890",
        "transcript": "Inbound caller is interested.",
        "summary": "Inbound call completed.",
        "started_at": datetime.now(timezone.utc),
        "ended_at": datetime.now(timezone.utc),
        "structured_data": {"interest_level": "Warm"},
    }
    payload.update(overrides)
    return RetellCallCompletedWebhook.model_validate(payload)


@pytest.mark.asyncio
async def test_inbound_call_matched_to_existing_lead(monkeypatch):
    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi",
        phone="+919876543210",
        language_preference=LanguagePreference.english,
    )
    async def fake_find_lead(phone, db):
        return lead

    monkeypatch.setattr(retell_service, "_find_lead_by_phone", fake_find_lead)

    found = await retell_service._find_lead_by_phone("+919876543210", object())

    assert found is lead


@pytest.mark.asyncio
async def test_inbound_call_creates_new_lead_if_not_found(monkeypatch):
    payload = inbound_payload(from_number="+919999999999")

    async def fake_create_zoho(phone, db):
        assert phone == "+919999999999"
        return "zoho-new"

    monkeypatch.setattr("app.services.zoho_service.create_zoho_lead_for_inbound", fake_create_zoho)

    class Db:
        def __init__(self):
            self.added = []

        def add(self, row):
            self.added.append(row)

        async def commit(self):
            return None

        async def refresh(self, row):
            return None

    db = Db()
    lead = await retell_service._create_inbound_lead(payload, db)

    assert lead.zoho_lead_id == "zoho-new"
    assert lead.name == "Unknown"
    assert lead.phone == "+919999999999"
    assert lead.source == "Inbound Call"


@pytest.mark.asyncio
async def test_inbound_call_updates_zoho(monkeypatch):
    captured = {}

    async def fake_token(db):
        return "token"

    async def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["json"] = kwargs["json"]

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"details": {"id": "zoho-new"}}]}

        return Response()

    monkeypatch.setattr("app.services.zoho_service.get_zoho_access_token", fake_token)
    monkeypatch.setattr("app.services.zoho_service._request_with_retry", fake_request)

    from app.services.zoho_service import create_zoho_lead_for_inbound

    zoho_id = await create_zoho_lead_for_inbound("+919876543210", object())

    assert zoho_id == "zoho-new"
    assert captured["method"] == "POST"
    assert captured["json"]["data"][0]["Lead_Source"] == "Inbound Call"


@pytest.mark.asyncio
async def test_inbound_direction_logged_in_call_attempts():
    attempt = CallAttempt(
        call_job_id=uuid4(),
        retell_call_id="inbound-call",
        attempt_number=1,
        status="completed",
        direction=CallDirection.inbound,
    )

    assert attempt.direction == CallDirection.inbound
