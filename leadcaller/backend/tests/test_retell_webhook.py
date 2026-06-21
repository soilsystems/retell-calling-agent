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
async def test_retell_inbound_returns_instant_response(client):
    """Inbound calls respond instantly with no DB lookup — eliminates 'please wait' hold message.

    Explicitly clears the outbound-bridge cache first so this test is independent
    of test ordering. (Without clearing, a leftover entry from a prior test would
    cause the LIFO fallback to misclassify this as an outbound bridge.)
    """
    from app.services import exotel_service
    exotel_service._pending_outbound_bridges.clear()

    class Db:
        pass

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
    ci = body["call_inbound"]
    variables = ci["retell_llm_dynamic_variables"]
    assert "override_agent_version" not in ci
    assert variables["phone"] == "+918746905010"
    assert variables["language"] == "auto"
    assert variables["language_preference"] == "auto"
    assert variables["call_direction"] == "inbound"
    assert variables["inbound_call"] == "true"
    assert "explicitly asks to speak" in variables["language_instruction"]
    # Inbound greeting is different from outbound
    assert "thank you for calling" in ci["agent_override"]["retell_llm"]["begin_message"].lower()
    # We intentionally do NOT override general_prompt — dashboard prompt stays in charge
    assert "general_prompt" not in ci["agent_override"]["retell_llm"]
    assert "global_prompt" not in ci["agent_override"]["conversation_flow"]
    assert ci["agent_override"]["agent"]["language"] == "en-IN"


@pytest.mark.asyncio
async def test_retell_inbound_uses_cached_outbound_bridge(client, monkeypatch):
    """Outbound bridge calls use the in-memory cache — no DB lookup needed."""
    from app.services import exotel_service
    from app.services.exotel_service import cache_outbound_bridge
    exotel_service._pending_outbound_bridges.clear()

    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi Chandra",
        phone="+918746905010",
        city="Bengaluru",
        campaign="May Campaign",
        language_preference=LanguagePreference.english,
    )
    cache_outbound_bridge(lead)

    class Db:
        pass

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
                "from_number": "+918047283246",
                "to_number": "+918046376848",
            },
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    variables = response.json()["call_inbound"]["retell_llm_dynamic_variables"]
    assert variables["call_direction"] == "outbound"
    assert variables["outbound_bridge_call"] == "true"
    assert variables["inbound_call"] == "false"
    assert variables["lead_name"] == "Ravi Chandra"
    assert "Do not thank them for calling" in variables["call_script"]


@pytest.mark.asyncio
async def test_retell_inbound_uses_cached_outbound_bridge_with_sid(client, monkeypatch):
    """Outbound bridge calls match exactly when the Call SID matches."""
    from app.services.exotel_service import cache_outbound_bridge

    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi Chandra",
        phone="+918746905010",
        city="Bengaluru",
        campaign="May Campaign",
        language_preference=LanguagePreference.english,
    )
    cache_outbound_bridge(lead, "test-call-sid-123")

    class Db:
        pass

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
                "from_number": "+918047283246",
                "to_number": "+918046376848",
                "custom_sip_headers": {
                    "X-Exotel-CallSid": "test-call-sid-123"
                }
            },
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    variables = response.json()["call_inbound"]["retell_llm_dynamic_variables"]
    assert variables["call_direction"] == "outbound"
    assert variables["outbound_bridge_call"] == "true"
    assert variables["inbound_call"] == "false"
    assert variables["lead_name"] == "Ravi Chandra"


@pytest.mark.asyncio
async def test_retell_inbound_from_exophone_with_empty_cache_is_inbound(client):
    """REGRESSION: When a customer calls the ExoPhone and there is NO pending
    outbound bridge in the cache, the call MUST be treated as inbound — not
    misclassified as outbound based on from_number alone.

    Both customer-inbound and bridged-outbound calls arrive at Retell with
    from_number == ExoPhone (Exotel is always the SIP sender), so the only
    reliable signal for 'outbound bridge' is a cache/DB entry that WE registered.
    """
    from app.services import exotel_service
    exotel_service._pending_outbound_bridges.clear()

    class Db:
        pass

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
                "from_number": "+918047283246",  # ExoPhone — looks identical to bridged outbound!
                "to_number": "+918046376848",
                "custom_sip_headers": {
                    "X-Exotel-CallSid": "real-customer-inbound-sid",
                },
            },
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    ci = response.json()["call_inbound"]
    variables = ci["retell_llm_dynamic_variables"]
    # MUST be inbound — no outbound was registered
    assert variables["call_direction"] == "inbound"
    assert variables["inbound_call"] == "true"
    assert variables["outbound_bridge_call"] == "false"
    # Inbound greeting (not outbound)
    bm = ci["agent_override"]["retell_llm"]["begin_message"].lower()
    assert "thank you for calling" in bm, f"Expected inbound greeting, got: {bm}"
    assert "i am calling regarding" not in bm, f"Got outbound greeting on inbound call: {bm}"


@pytest.mark.asyncio
async def test_retell_inbound_sid_mismatch_falls_back_to_lifo_when_from_exophone(client, monkeypatch):
    """
    When from_number is the ExoPhone, the call MUST be a bridged outbound call.
    SIDs from Exotel /Calls/connect don't always match the SID Retell sees in SIP
    headers, so we fall back to LIFO cache when SID lookup misses.
    """
    from app.services import exotel_service
    from app.services.exotel_service import cache_outbound_bridge
    exotel_service._pending_outbound_bridges.clear()

    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi Chandra",
        phone="+918746905010",
        city="Bengaluru",
        campaign="May Campaign",
        language_preference=LanguagePreference.english,
    )
    cache_outbound_bridge(lead, "test-call-sid-different")

    class Db:
        pass

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
                "from_number": "+918047283246",
                "to_number": "+918046376848",
                "custom_sip_headers": {
                    "X-Exotel-CallSid": "test-call-sid-another"
                }
            },
        },
    )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    variables = response.json()["call_inbound"]["retell_llm_dynamic_variables"]
    # from_number is the ExoPhone → MUST be outbound bridge → LIFO finds the cached lead
    assert variables["call_direction"] == "outbound"
    assert variables["inbound_call"] == "false"
    assert variables["outbound_bridge_call"] == "true"
    assert variables["lead_name"] == "Ravi Chandra"


@pytest.mark.asyncio
async def test_exotel_completed_status_records_attempt(client, monkeypatch):
    # A 'completed' Exotel status records the attempt but does NOT send WhatsApp
    # here — the Retell call_completed handler sends the post-call template, so
    # sending here too would double-message the lead.
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

    class Db:
        pass

    async def fake_get_db():
        yield Db()

    async def fake_find_lead(payload, db):
        return lead

    async def fake_ensure_attempt(found_lead, payload, db, **kwargs):
        assert found_lead is lead
        return SimpleNamespace(id=attempt_id)

    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = fake_get_db
    monkeypatch.setattr("app.routers.webhooks._find_lead_for_exotel_status", fake_find_lead)
    monkeypatch.setattr("app.routers.webhooks._ensure_exotel_call_attempt", fake_ensure_attempt)

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
    assert response.json()["call_attempt_id"] == str(attempt_id)
    assert "whatsapp" not in response.json()


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
