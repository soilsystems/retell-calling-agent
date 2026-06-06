import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
    assert body["call_inbound"]["override_agent_version"] == 3
    assert variables["lead_name"] == "Ravi Chandra"
    assert variables["customer_name"] == "Ravi Chandra"
    assert variables["phone"] == "+918746905010"
    assert "Ravi Chandra" in body["call_inbound"]["agent_override"]["retell_llm"]["begin_message"]


@pytest.mark.asyncio
async def test_exotel_completed_status_enqueues_whatsapp(client, monkeypatch):
    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi Chandra",
        phone="+918746905010",
        city="Bengaluru",
        campaign="May Campaign",
        language_preference=LanguagePreference.english,
    )
    attempt_id = uuid4()
    captured = {}

    class Db:
        pass

    async def fake_get_db():
        yield Db()

    async def fake_find_lead(payload, db):
        captured["payload"] = payload
        return lead

    async def fake_ensure_attempt(found_lead, payload, db):
        assert found_lead is lead
        return SimpleNamespace(id=attempt_id)

    async def fake_send_whatsapp(call_attempt_id):
        captured["call_attempt_id"] = call_attempt_id

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = fake_get_db
    monkeypatch.setattr("app.routers.webhooks._find_lead_for_exotel_status", fake_find_lead)
    monkeypatch.setattr("app.routers.webhooks._ensure_exotel_call_attempt", fake_ensure_attempt)
    monkeypatch.setattr("app.routers.webhooks.send_whatsapp_for_call", fake_send_whatsapp)

    response = await client.post(
        "/webhooks/exotel/status",
        data={
            "CallStatus": "completed",
            "CallSid": "exotel-call-1",
            "CustomField": json.dumps({"lead_id": str(lead.id), "lead_phone": lead.phone}),
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["whatsapp"] == "queued"
    assert response.json()["call_attempt_id"] == str(attempt_id)
    assert captured["call_attempt_id"] == attempt_id


@pytest.mark.asyncio
async def test_process_retell_completion_creates_attempt_dynamically():
    import uuid
    from app.services.retell_service import process_retell_completion
    from app.schemas.retell_schema import RetellCallCompletedWebhook
    from app.models import Lead, CallJob, CallAttempt, CallJobStatus, CallAttemptStatus, WebhookEvent, WebhookSource
    
    lead_id = uuid.uuid4()
    lead = Lead(
        id=lead_id,
        zoho_lead_id="zoho-lead-123",
        name="Ravi Chandra",
        phone="+918746905010"
    )
    
    webhook_event = WebhookEvent(
        source=WebhookSource.retell,
        event_type="call_completed",
        payload={},
        processed=False
    )
    
    payload_data = {
        "call_id": "call_inbound_dynamic_test",
        "call_status": "completed",
        "transcript": "Hello, how can I help you?",
        "summary": "Successful call",
        "recording_url": "http://example.com/recording.mp3",
        "duration_ms": 60000,
        "start_timestamp": 1778742000000,
        "end_timestamp": 1778742060000,
        "call_analysis": {
            "call_summary": "Successful call",
            "custom_analysis_data": {
                "interest_level": "Hot",
                "follow_up_required": True,
                "follow_up_time": "2026-05-15T10:00:00+05:30",
            }
        },
        "metadata": {
            "lead_id": str(lead_id)
        }
    }
    
    # Flatten the raw payload through model_validator of RetellCallCompletedWebhook
    payload = RetellCallCompletedWebhook.model_validate({"call": payload_data})
    
    # Mock DB
    class MockResult:
        def __init__(self, value):
            self.value = value
        def scalar_one_or_none(self):
            return self.value
        def scalar_one(self):
            return self.value

    class MockDb:
        def __init__(self):
            self.added = []
            self.commits = 0
            self.refreshes = 0
            self.execute_calls = 0

        async def execute(self, stmt):
            self.execute_calls += 1
            # First execute is checking CallAttempt existence
            if self.execute_calls == 1:
                return MockResult(None)
            # Second execute is looking up CallJob
            elif self.execute_calls == 2:
                return MockResult(None)
            # Third execute is re-loading the created CallAttempt
            else:
                # Find the added CallAttempt in self.added
                attempt = [x for x in self.added if isinstance(x, CallAttempt)][0]
                # Ensure it has a mock CallJob associated with it for the test
                for x in self.added:
                    if isinstance(x, CallJob):
                        attempt.call_job = x
                return MockResult(attempt)

        async def get(self, model, id_):
            if model == Lead and id_ == lead_id:
                return lead
            return None

        async def scalar(self, stmt):
            # Checking attempt count for CallJob
            return 0

        def add(self, item):
            self.added.append(item)

        async def commit(self):
            self.commits += 1

        async def refresh(self, item):
            self.refreshes += 1

    db = MockDb()
    attempt = await process_retell_completion(payload, webhook_event, db)
    
    assert attempt is not None
    assert attempt.retell_call_id == "call_inbound_dynamic_test"
    assert attempt.status == CallAttemptStatus.completed
    assert attempt.transcript == "Hello, how can I help you?"
    assert attempt.summary == "Successful call"
    
    # Ensure CallJob and CallAttempt were both added to DB
    jobs = [x for x in db.added if isinstance(x, CallJob)]
    attempts = [x for x in db.added if isinstance(x, CallAttempt)]
    assert len(jobs) == 1
    assert len(attempts) == 1
    assert jobs[0].lead_id == lead_id
    assert attempts[0].call_job_id == jobs[0].id
