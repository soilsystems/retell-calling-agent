import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.models import CallJob, Lead, LanguagePreference
from app.services import meta_service, zoho_service
from app.utils.phone import format_phone_e164


def _meta_signature(payload: bytes) -> str:
    digest = hmac.new(
        os.environ["META_APP_SECRET"].encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def _meta_payload(leadgen_id: str = "META_LEAD_1") -> dict:
    return {
        "object": "page",
        "entry": [
            {
                "id": "PAGE_ID",
                "time": 1234567890,
                "changes": [
                    {
                        "value": {
                            "form_id": "FORM_ID",
                            "leadgen_id": leadgen_id,
                            "created_time": 1234567890,
                            "page_id": "PAGE_ID",
                        },
                        "field": "leadgen",
                    }
                ],
            }
        ],
    }


class FakeDb:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


def _lead() -> Lead:
    return Lead(
        id=uuid.uuid4(),
        zoho_lead_id="ZOHO_META_1",
        name="Meta Lead",
        phone="+919137500132",
        email="meta@example.com",
        city="Bengaluru",
        language_preference=LanguagePreference.english,
        source="Meta Ads",
        campaign="AD123",
    )


async def _patch_successful_process(monkeypatch, *, lead: Lead | None = None):
    calls: dict[str, object] = {"records": 0, "processed": 0, "zoho": 0, "upsert": 0}
    lead = lead or _lead()

    async def check_exists(db, leadgen_id, source="meta"):
        return False

    async def record_event(db, leadgen_id, source, event_type, payload):
        calls["records"] = int(calls["records"]) + 1

    async def create_zoho(lead_data, db):
        calls["zoho"] = int(calls["zoho"]) + 1
        return lead.zoho_lead_id

    async def upsert(payload, db):
        calls["upsert"] = int(calls["upsert"]) + 1
        return lead

    async def mark_processed(db, leadgen_id):
        calls["processed"] = int(calls["processed"]) + 1

    async def trigger_call(call_job_id, db):
        calls["triggered"] = str(call_job_id)

    async def resolve_page_credentials():
        return "PAGE_ID", "page-token"

    monkeypatch.setattr(meta_service, "check_webhook_event_exists", check_exists)
    monkeypatch.setattr(meta_service, "record_webhook_event", record_event)
    monkeypatch.setattr(meta_service, "create_lead_in_zoho", create_zoho)
    monkeypatch.setattr(meta_service, "upsert_lead", upsert)
    monkeypatch.setattr(meta_service, "mark_webhook_processed", mark_processed)
    monkeypatch.setattr(meta_service, "trigger_new_lead_call", trigger_call)
    monkeypatch.setattr(meta_service, "_resolve_page_credentials", resolve_page_credentials)
    monkeypatch.setattr(meta_service, "is_business_hours", lambda now: True)
    return calls


@pytest.mark.asyncio
async def test_meta_verification_returns_challenge(client):
    response = await client.get(
        "/webhooks/meta/new-lead",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "soilsystems_meta_verify_2026",
            "hub.challenge": "testchallenge123",
        },
    )

    assert response.status_code == 200
    assert response.text == "testchallenge123"


@pytest.mark.asyncio
async def test_meta_verification_wrong_token_returns_403(client):
    response = await client.get(
        "/webhooks/meta/new-lead",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "testchallenge123",
        },
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_meta_signature_valid_accepted(client, monkeypatch):
    captured = {}

    async def fake_process(data):
        captured["data"] = data

    monkeypatch.setattr("app.routers.meta_webhook.process_meta_lead", fake_process)
    body = json.dumps(_meta_payload("META_VALID")).encode("utf-8")

    response = await client.post(
        "/webhooks/meta/new-lead",
        content=body,
        headers={"X-Hub-Signature-256": _meta_signature(body)},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert captured["data"]["entry"][0]["changes"][0]["value"]["leadgen_id"] == "META_VALID"


@pytest.mark.asyncio
async def test_meta_signature_invalid_returns_200_without_processing(client, monkeypatch):
    captured = {"processed": False}

    async def fake_process(data):
        captured["processed"] = True

    monkeypatch.setattr("app.routers.meta_webhook.process_meta_lead", fake_process)
    body = json.dumps(_meta_payload("META_INVALID")).encode("utf-8")
    response = await client.post(
        "/webhooks/meta/new-lead",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert captured["processed"] is False


@pytest.mark.asyncio
async def test_meta_lead_processed_end_to_end(monkeypatch):
    calls = await _patch_successful_process(monkeypatch)
    db = FakeDb()

    with respx.mock(assert_all_called=True) as router:
        router.get("https://graph.facebook.com/v19.0/META123").mock(
            return_value=httpx.Response(
                200,
                json={
                    "ad_id": "AD123",
                    "field_data": [
                        {"name": "full_name", "values": ["Meta Lead"]},
                        {"name": "phone_number", "values": ["9137500132"]},
                        {"name": "email", "values": ["meta@example.com"]},
                        {"name": "city", "values": ["Bengaluru"]},
                    ],
                },
            )
        )
        await meta_service._process_meta_lead(_meta_payload("META123"), db)

    call_jobs = [item for item in db.added if isinstance(item, CallJob)]
    assert calls["records"] == 1
    assert calls["processed"] == 1
    assert calls["zoho"] == 1
    assert calls["upsert"] == 1
    assert calls["triggered"] == str(call_jobs[0].id)
    assert len(call_jobs) == 1
    assert call_jobs[0].trigger_reason == "new_lead_meta"


@pytest.mark.asyncio
async def test_duplicate_leadgen_id_skipped(monkeypatch):
    db = FakeDb()

    async def exists(db, leadgen_id, source="meta"):
        return True

    async def should_not_run(*args, **kwargs):
        raise AssertionError("duplicate lead should stop processing")

    monkeypatch.setattr(meta_service, "check_webhook_event_exists", exists)
    monkeypatch.setattr(meta_service, "record_webhook_event", should_not_run)

    await meta_service._process_meta_lead(_meta_payload("META_DUPLICATE"), db)

    assert db.added == []


def test_phone_10_digits_formatted_to_e164():
    assert format_phone_e164("9137500132") == "+919137500132"


def test_phone_with_plus91_kept_as_is():
    assert format_phone_e164("+919137500132") == "+919137500132"


def test_phone_with_0_prefix_formatted():
    assert format_phone_e164("09137500132") == "+919137500132"


def test_phone_placeholder_returns_empty():
    assert format_phone_e164("<test lead: dummy data for phone_number>") == ""


def test_meta_field_aliases_map_to_internal_lead_data():
    lead_data = meta_service._normalize_meta_lead_fields(
        {
            "form_id": "FORM123",
            "ad_id": "AD123",
            "ad_name": "Woods Test Ad",
            "campaign_id": "CAMPAIGN123",
            "campaign_name": "Woods Launch Campaign",
            "field_data": [
                {"name": "your_name", "values": ["Mapped Lead"]},
                {"name": "mobile_number", "values": ["9876543210"]},
                {"name": "email_address", "values": ["mapped@example.com"]},
                {"name": "location", "values": ["Mysuru"]},
                {"name": "preferred_language", "values": ["English"]},
            ],
        },
        "META_MAPPED",
    )

    assert lead_data["lead_id"] == "META_MAPPED"
    assert lead_data["name"] == "Mapped Lead"
    assert lead_data["phone"] == "+919876543210"
    assert lead_data["email"] == "mapped@example.com"
    assert lead_data["city"] == "Mysuru"
    assert lead_data["language_preference"] == "english"
    assert lead_data["campaign"] == "Woods Launch Campaign"
    assert lead_data["meta_form_id"] == "FORM123"
    assert lead_data["meta_ad_id"] == "AD123"
    assert lead_data["meta_ad_name"] == "Woods Test Ad"


@pytest.mark.asyncio
async def test_missing_phone_handled_gracefully(monkeypatch):
    calls = {"processed": 0, "zoho": 0, "upsert_no_phone": 0}
    db = FakeDb()

    async def exists(db, leadgen_id, source="meta"):
        return False

    async def record(*args, **kwargs):
        return None

    async def fetch(leadgen_id):
        return {"lead_id": leadgen_id, "name": "No Phone", "phone": ""}

    async def create_zoho(*args, **kwargs):
        calls["zoho"] += 1
        return "ZOHO_NO_PHONE"

    async def mark(db, leadgen_id):
        calls["processed"] += 1

    async def upsert_no_phone(lead_data, db):
        calls["upsert_no_phone"] += 1
        return _lead()

    monkeypatch.setattr(meta_service, "check_webhook_event_exists", exists)
    monkeypatch.setattr(meta_service, "record_webhook_event", record)
    monkeypatch.setattr(meta_service, "fetch_meta_lead_data", fetch)
    monkeypatch.setattr(meta_service, "create_lead_in_zoho", create_zoho)
    monkeypatch.setattr(meta_service, "mark_webhook_processed", mark)
    monkeypatch.setattr(meta_service, "_upsert_meta_lead_without_phone", upsert_no_phone)

    await meta_service._process_meta_lead(_meta_payload("META_NO_PHONE"), db)

    assert calls == {"processed": 1, "zoho": 1, "upsert_no_phone": 1}
    assert not [item for item in db.added if isinstance(item, CallJob)]


@pytest.mark.asyncio
async def test_lead_created_in_zoho(monkeypatch):
    captured = {}
    db = FakeDb()

    class FakeResponse:
        status_code = 201
        text = '{"ok": true}'

        def json(self):
            return {"data": [{"details": {"id": "ZOHO_CREATED"}}]}

    async def fake_token(db):
        return "zoho-token"

    async def fake_request(method, url, *, headers=None, json=None, data=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(zoho_service, "get_zoho_access_token", fake_token)
    monkeypatch.setattr(zoho_service, "_request_with_retry", fake_request)

    zoho_id = await zoho_service.create_lead_in_zoho(
        {
            "name": "Meta Lead",
            "phone": "+919137500132",
            "email": "meta@example.com",
            "city": "Bengaluru",
            "campaign": "AD123",
            "source": "Meta Ads",
        },
        db,
    )

    assert zoho_id == "ZOHO_CREATED"
    assert captured["method"] == "POST"
    assert captured["json"]["data"][0]["Lead_Source"] == "Meta Ads"
    assert captured["json"]["data"][0]["First_Name"] == "Meta"
    assert captured["json"]["data"][0]["Last_Name"] == "Lead"
    assert captured["json"]["data"][0]["Lead_Status"] == "New"
    assert captured["json"]["data"][0]["Campaign_Source__c"] == "AD123"
    assert captured["json"]["data"][0]["Phone"] == "+919137500132"
    assert captured["json"]["data"][0]["Mobile"] == "+919137500132"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_zoho_create_omits_phone_fields_when_missing(monkeypatch):
    captured = {}
    db = FakeDb()

    class FakeResponse:
        status_code = 201
        text = '{"ok": true}'

        def json(self):
            return {"data": [{"details": {"id": "ZOHO_NO_PHONE"}}]}

    async def fake_token(db):
        return "zoho-token"

    async def fake_request(method, url, *, headers=None, json=None, data=None):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(zoho_service, "get_zoho_access_token", fake_token)
    monkeypatch.setattr(zoho_service, "_request_with_retry", fake_request)

    await zoho_service.create_lead_in_zoho(
        {
            "name": "No Phone",
            "phone": "",
            "email": "meta@example.com",
            "city": "Bengaluru",
            "campaign": "AD123",
            "source": "Meta Ads",
        },
        db,
    )

    assert "Phone" not in captured["json"]["data"][0]
    assert "Mobile" not in captured["json"]["data"][0]


@pytest.mark.asyncio
async def test_lead_upserted_in_supabase(monkeypatch):
    calls = await _patch_successful_process(monkeypatch)
    db = FakeDb()

    async def fetch(leadgen_id):
        return {
            "lead_id": leadgen_id,
            "name": "Meta Lead",
            "phone": "+919137500132",
            "email": "meta@example.com",
            "city": "Bengaluru",
            "source": "Meta Ads",
            "campaign": "AD123",
            "language_preference": "english",
        }

    monkeypatch.setattr(meta_service, "fetch_meta_lead_data", fetch)

    await meta_service._process_meta_lead(_meta_payload("META_UPSERT"), db)

    assert calls["upsert"] == 1


@pytest.mark.asyncio
async def test_call_job_created_after_meta_lead(monkeypatch):
    await _patch_successful_process(monkeypatch)
    db = FakeDb()

    async def fetch(leadgen_id):
        return {
            "lead_id": leadgen_id,
            "name": "Meta Lead",
            "phone": "+919137500132",
            "email": "",
            "city": "",
            "source": "Meta Ads",
            "campaign": "",
            "language_preference": "english",
        }

    monkeypatch.setattr(meta_service, "fetch_meta_lead_data", fetch)

    await meta_service._process_meta_lead(_meta_payload("META_JOB"), db)

    call_jobs = [item for item in db.added if isinstance(item, CallJob)]
    assert len(call_jobs) == 1
    assert call_jobs[0].status.value == "pending"
    assert call_jobs[0].trigger_reason == "new_lead_meta"


@pytest.mark.asyncio
async def test_outside_business_hours_new_lead_schedules_next_slot(monkeypatch):
    next_slot = datetime(2026, 6, 8, 3, 30, tzinfo=timezone.utc)
    await _patch_successful_process(monkeypatch)
    db = FakeDb()

    async def fetch(leadgen_id):
        return {
            "lead_id": leadgen_id,
            "name": "Meta Lead",
            "phone": "+919137500132",
            "email": "",
            "city": "",
            "source": "Meta Ads",
            "campaign": "",
            "language_preference": "english",
        }

    monkeypatch.setattr(meta_service, "fetch_meta_lead_data", fetch)
    monkeypatch.setattr(meta_service, "is_business_hours", lambda now: False)
    monkeypatch.setattr(meta_service, "next_business_slot", lambda now: next_slot)

    await meta_service._process_meta_lead(_meta_payload("META_AFTER_HOURS"), db)

    call_job = next(item for item in db.added if isinstance(item, CallJob))
    assert call_job.scheduled_at == next_slot
    assert call_job.trigger_reason == "new_lead_meta"
