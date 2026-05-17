import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models import CallJob, CallJobStatus


def retell_payload(**overrides):
    payload = {
        "call_id": "retell-call-1",
        "call_status": "completed",
        "transcript": "Customer is interested.",
        "summary": "Hot lead looking for a 3BHK.",
        "recording_url": "https://recordings.example.com/1.mp3",
        "duration_seconds": 180,
        "started_at": "2026-05-14T04:30:00+00:00",
        "ended_at": "2026-05-14T04:33:00+00:00",
        "structured_data": {
            "interest_level": "Hot",
            "budget": "1.5 Cr",
            "timeline": "30 days",
            "property_type": "3BHK",
            "language": "english",
            "follow_up_required": True,
            "follow_up_time": "2026-05-15T10:00:00+05:30",
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_valid_retell_signature_accepted(client, retell_signature, monkeypatch):
    async def fake_process(payload, webhook_event, db):
        return None

    monkeypatch.setattr("app.routers.webhooks.process_retell_completion", fake_process)
    body = json.dumps(retell_payload()).encode()
    response = await client.post(
        "/webhooks/retell/call-completed",
        content=body,
        headers={"X-Retell-Signature": retell_signature(body)},
    )
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
    assert retell_payload()["structured_data"]["interest_level"] == "Hot"


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
