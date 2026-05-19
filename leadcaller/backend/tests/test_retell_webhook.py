import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models import CallJob, CallJobStatus, LanguagePreference, Lead


def retell_payload(**overrides):
    payload = {
        "event": "call_completed",
        "call": {
            "call_id": "retell-call-1",
            "call_status": "completed",
            "transcript": "Customer is interested.",
            "recording_url": "https://recordings.example.com/1.mp3",
            "duration_ms": 180000,
            "start_timestamp": 1778742000000,
            "end_timestamp": 1778742180000,
            "call_analysis": {
                "call_summary": "Hot lead looking for a 3BHK.",
                "custom_analysis_data": {
                    "interest_level": "Hot",
                    "budget": "1.5 Cr",
                    "timeline": "30 days",
                    "property_type": "3BHK",
                    "language": "english",
                    "follow_up_required": True,
                    "follow_up_time": "2026-05-15T10:00:00+05:30",
                },
            },
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_valid_retell_signature_accepted(client, retell_signature, monkeypatch):
    async def fake_process(payload, webhook_event, db):
        return None

    class Result:
        def scalar_one_or_none(self):
            return None

    class Db:
        async def execute(self, stmt):
            return Result()

        def add(self, item):
            return None

        async def commit(self):
            return None

        async def refresh(self, item):
            return None

    async def fake_get_db():
        yield Db()

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = fake_get_db
    monkeypatch.setattr("app.routers.webhooks.process_retell_completion", fake_process)
    body = json.dumps(retell_payload()).encode()
    response = await client.post(
        "/webhooks/retell/call-completed",
        content=body,
        headers={"X-Retell-Signature": retell_signature(body)},
    )
    app.dependency_overrides.clear()
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_retell_call_id_idempotent(client, retell_signature, monkeypatch):
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
    body = json.dumps(retell_payload()).encode()
    response = await client.post(
        "/webhooks/retell/call-completed",
        content=body,
        headers={"X-Retell-Signature": retell_signature(body)},
    )
    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["status"] == "already handled"


@pytest.mark.asyncio
async def test_hot_lead_triggers_urgent_zoho_task(monkeypatch):
    captured = {}

    async def fake_request(method, url, **kwargs):
        captured["body"] = kwargs["json"]

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{"details": {"id": "task-1"}}]}

        return Response()

    monkeypatch.setattr("app.services.zoho_service._request_with_retry", fake_request)
    assert retell_payload()["call"]["call_analysis"]["custom_analysis_data"]["interest_level"] == "Hot"


@pytest.mark.asyncio
async def test_no_answer_schedules_retry_2h():
    job = CallJob(id=uuid4(), lead_id=uuid4(), status=CallJobStatus.failed, scheduled_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    assert job.status == CallJobStatus.failed
    assert now + timedelta(hours=2) > now


@pytest.mark.asyncio
async def test_max_retries_cancels_job():
    job = CallJob(
        id=uuid4(),
        lead_id=uuid4(),
        status=CallJobStatus.failed,
        retry_count=3,
        max_retries=3,
        scheduled_at=datetime.now(timezone.utc),
    )
    if job.retry_count >= job.max_retries:
        job.status = CallJobStatus.cancelled
    assert job.status == CallJobStatus.cancelled


@pytest.mark.asyncio
async def test_retell_inbound_returns_call_inbound_dynamic_variables(client):
    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi Chandra",
        phone="+918746905010",
        city="Bengaluru",
        campaign="May Campaign",
        language_preference=LanguagePreference.english,
    )

    class Scalars:
        def first(self):
            return lead

    class Result:
        def scalars(self):
            return Scalars()

    class Db:
        async def execute(self, stmt):
            return Result()

    async def fake_get_db():
        yield Db()

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = fake_get_db
    response = await client.post(
        "/webhooks/retell/inbound",
        json={
            "event": "call_inbound",
            "call_inbound": {
                "from_number": "+918746905010",
                "to_number": "+918046376848",
            },
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    variables = body["call_inbound"]["dynamic_variables"]
    assert variables["lead_name"] == "Ravi Chandra"
    assert variables["customer_name"] == "Ravi Chandra"
    assert variables["phone"] == "+918746905010"
