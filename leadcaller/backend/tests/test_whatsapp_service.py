from types import SimpleNamespace
from uuid import uuid4

import pytest
import respx
from httpx import Response

from app.models import CallAttempt, CallAttemptStatus, CallJob, CallJobStatus, LanguagePreference, Lead, WhatsAppLogStatus
from app.services import whatsapp_service
from app.services.whatsapp_service import (
    BROCHURE_TEMPLATE,
    FOLLOWUP_TEMPLATE,
    SITE_VISIT_TEMPLATE,
    format_wati_phone,
    send_whatsapp_for_call,
)


class FakeDb:
    def __init__(self):
        self.rows = []
        self.commits = 0

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        self.commits += 1


def make_attempt(phone="+919876543210", name="Rahul", structured=None):
    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name=name,
        phone=phone,
        city="Bengaluru",
        campaign="Windflower",
        language_preference=LanguagePreference.english,
    )
    job = CallJob(
        id=uuid4(),
        lead_id=lead.id,
        lead=lead,
        status=CallJobStatus.completed,
        scheduled_at=whatsapp_service._utcnow(),
    )
    return CallAttempt(
        id=uuid4(),
        call_job_id=job.id,
        call_job=job,
        retell_call_id="retell-call-1",
        attempt_number=1,
        status=CallAttemptStatus.completed,
        structured_data=structured or {},
    )


@pytest.fixture
def exotel_wa_settings(monkeypatch):
    settings = SimpleNamespace(
        EXOTEL_WA_SUBDOMAIN="api.in.exotel.com",
        EXOTEL_WA_ACCOUNT_SID="test-account-sid",
        EXOTEL_WA_API_KEY="test-api-key",
        EXOTEL_WA_API_TOKEN="test-api-token",
        EXOTEL_WA_PHONE_NUMBER="+918047283246",
        BOOKING_LINK="https://soilsystems.in/book",
        EXOTEL_WA_TEMPLATE_COMPLETED="call_followup",
        EXOTEL_WA_TEMPLATE_MISSED="call_missed",
    )
    monkeypatch.setattr("app.services.whatsapp_service.get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def mock_sleep(monkeypatch):
    async def fake_sleep(seconds):
        return None
    monkeypatch.setattr("app.services.whatsapp_service.asyncio.sleep", fake_sleep)


@pytest.fixture
def disable_zoho_update(monkeypatch):
    async def fake_update(log_id, db, retry_once=True):
        return None

    monkeypatch.setattr("app.services.zoho_service.update_zoho_whatsapp_status", fake_update)


async def run_for_attempt(monkeypatch, attempt, db=None):
    async def fake_load_attempt(call_attempt_id, session):
        return attempt

    monkeypatch.setattr("app.services.whatsapp_service._load_attempt", fake_load_attempt)
    db = db or FakeDb()
    await send_whatsapp_for_call(attempt.id, db)
    return db


def assert_sent_log(db, template_name):
    log = db.rows[-1]
    assert log.status == WhatsAppLogStatus.sent
    assert log.template_name == template_name
    return log


@pytest.mark.asyncio
async def test_site_visit_agreed_sends_template_1(monkeypatch, exotel_wa_settings, disable_zoho_update):
    attempt = make_attempt(structured={"site_visit_agreed": True, "site_visit_day": "Saturday"})
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(200, json={"message": {"sid": "msg-123"}})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert_sent_log(db, SITE_VISIT_TEMPLATE)
    body = route.calls.last.request.read().decode()
    assert "soil_systems_site_visit" in body
    assert "Saturday" in body


@pytest.mark.asyncio
async def test_followup_required_sends_template_2(monkeypatch, exotel_wa_settings, disable_zoho_update):
    attempt = make_attempt(structured={"follow_up_required": True, "follow_up_time": "2026-05-21T10:00:00+05:30"})
    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(200, json={"message": {"sid": "msg-123"}})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert_sent_log(db, FOLLOWUP_TEMPLATE)


@pytest.mark.asyncio
async def test_hot_lead_no_site_visit_sends_template_3(monkeypatch, exotel_wa_settings, disable_zoho_update):
    attempt = make_attempt(structured={"interest_level": "Hot"})
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(200, json={"message": {"sid": "msg-123"}})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert_sent_log(db, BROCHURE_TEMPLATE)
    body = route.calls.last.request.read().decode()
    assert "soil_systems_brochure" in body
    assert "https://soilsystems.in/book" in body


@pytest.mark.asyncio
async def test_warm_lead_no_site_visit_sends_template_3(monkeypatch, exotel_wa_settings, disable_zoho_update):
    attempt = make_attempt(structured={"interest_level": "Warm"})
    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(200, json={"message": {"sid": "msg-123"}})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert_sent_log(db, BROCHURE_TEMPLATE)


@pytest.mark.asyncio
async def test_cold_lead_skips_whatsapp(monkeypatch):
    attempt = make_attempt(structured={"interest_level": "Cold"})
    db = await run_for_attempt(monkeypatch, attempt)

    log = db.rows[-1]
    assert log.status == WhatsAppLogStatus.skipped
    assert log.template_name is None
    assert log.error_message == "interest_level=Cold"


@pytest.mark.asyncio
async def test_not_interested_skips_whatsapp(monkeypatch):
    attempt = make_attempt(structured={"interest_level": "Not Interested"})
    db = await run_for_attempt(monkeypatch, attempt)

    log = db.rows[-1]
    assert log.status == WhatsAppLogStatus.skipped
    assert log.error_message == "interest_level=Not Interested"


@pytest.mark.asyncio
async def test_site_visit_true_takes_priority_over_followup(monkeypatch, exotel_wa_settings, disable_zoho_update):
    attempt = make_attempt(
        structured={"site_visit_agreed": True, "follow_up_required": True, "site_visit_day": "Sunday"}
    )
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(200, json={"message": {"sid": "msg-123"}})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert_sent_log(db, SITE_VISIT_TEMPLATE)
    assert "soil_systems_followup" not in route.calls.last.request.read().decode()


@pytest.mark.asyncio
async def test_invalid_phone_logs_skipped(monkeypatch):
    attempt = make_attempt(phone="080-12345678", structured={"interest_level": "Hot"})
    db = await run_for_attempt(monkeypatch, attempt)

    log = db.rows[-1]
    assert log.status == WhatsAppLogStatus.skipped
    assert log.error_message == "invalid or missing phone number"


@pytest.mark.asyncio
async def test_exotel_api_failure_logs_failed(monkeypatch, exotel_wa_settings, disable_zoho_update):
    attempt = make_attempt(structured={"interest_level": "Hot"})

    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(500, json={"error": "down"})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert db.rows[-1].status == WhatsAppLogStatus.failed
    assert "Exotel API failed with status 500" in db.rows[-1].error_message


@pytest.mark.asyncio
async def test_exotel_retry_on_failure(monkeypatch, exotel_wa_settings, disable_zoho_update):
    attempt = make_attempt(structured={"interest_level": "Hot"})

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            side_effect=[
                Response(500, json={"error": "down"}),
                Response(200, json={"message": {"sid": "msg-123"}}),
            ]
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert len(route.calls) == 2
    assert db.rows[0].status == WhatsAppLogStatus.failed
    assert db.rows[-1].status == WhatsAppLogStatus.sent


def test_phone_formatted_correctly_removes_plus():
    assert format_wati_phone("+919876543210") == "919876543210"


def test_phone_formatted_correctly_adds_91_prefix():
    assert format_wati_phone("9876543210") == "919876543210"


@pytest.mark.asyncio
async def test_zoho_updated_after_whatsapp_sent(monkeypatch, exotel_wa_settings):
    attempt = make_attempt(structured={"interest_level": "Warm"})
    captured = {}

    async def fake_update(log_id, db, retry_once=True):
        captured["log_id"] = log_id

    monkeypatch.setattr("app.services.zoho_service.update_zoho_whatsapp_status", fake_update)
    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(200, json={"message": {"sid": "msg-123"}})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert captured["log_id"] == db.rows[-1].id


@pytest.mark.asyncio
async def test_exotel_payload_structure(monkeypatch, exotel_wa_settings, disable_zoho_update):
    import json
    attempt = make_attempt(structured={"site_visit_agreed": True, "site_visit_day": "Saturday"})
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.in.exotel.com/v2/accounts/test-account-sid/messages").mock(
            return_value=Response(200, json={"message": {"sid": "msg-123"}})
        )
        await run_for_attempt(monkeypatch, attempt)

    request_payload = json.loads(route.calls.last.request.read().decode())
    assert "from" in request_payload
    assert request_payload["from"] == "+918047283246"
    assert request_payload["to"] == "+919876543210"
    assert "content" in request_payload
    assert request_payload["content"]["type"] == "template"
    assert request_payload["content"]["template"]["name"] == "soil_systems_site_visit"
    assert request_payload["content"]["template"]["language"]["policy"] == "deterministic"

