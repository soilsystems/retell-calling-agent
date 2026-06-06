import base64
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import respx
from httpx import Response

from app.models import CallAttempt, CallAttemptStatus, CallJob, CallJobStatus, LanguagePreference, Lead, WhatsAppLogStatus
from app.services import whatsapp_service
from app.services.whatsapp_service import (
    SOIL_SYSTEMS_TEMPLATE,
    build_exotel_template_payload,
    format_phone_for_exotel_whatsapp,
    format_wati_phone,
    send_whatsapp_for_call,
)


EXOTEL_URL = "https://api.in.exotel.com/v2/accounts/test-account-sid/messages"


class FakeDb:
    def __init__(self):
        self.rows = []
        self.commits = 0

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        self.commits += 1


def make_attempt(phone="+919876543210", name="Rahul"):
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
        structured_data={"interest_level": "Hot"},
    )


@pytest.fixture
def exotel_wa_settings(monkeypatch):
    settings = SimpleNamespace(
        EXOTEL_ACCOUNT_SID="fallback-account",
        EXOTEL_API_KEY="fallback-key",
        EXOTEL_API_TOKEN="fallback-token",
        EXOTEL_SUBDOMAIN="api.exotel.com",
        EXOTEL_WHATSAPP_NUMBER=None,
        EXOTEL_WHATSAPP_FROM_NUMBER="+918047283246",
        EXOTEL_WA_SUBDOMAIN="api.in.exotel.com",
        EXOTEL_WA_ACCOUNT_SID="test-account-sid",
        EXOTEL_WA_API_KEY="test-api-key",
        EXOTEL_WA_API_TOKEN="test-api-token",
        EXOTEL_WA_PHONE_NUMBER="+918047283246",
        EXOTEL_WA_TEMPLATE_SOIL_SYSTEMS=SOIL_SYSTEMS_TEMPLATE,
    )
    monkeypatch.setattr("app.services.whatsapp_service.get_settings", lambda: settings)
    return settings


@pytest.fixture(autouse=True)
def mock_sleep(monkeypatch):
    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("app.services.whatsapp_service.asyncio.sleep", fake_sleep)


async def run_for_attempt(monkeypatch, attempt, db=None):
    async def fake_load_attempt(call_attempt_id, session):
        return attempt

    monkeypatch.setattr("app.services.whatsapp_service._load_attempt", fake_load_attempt)
    db = db or FakeDb()
    await send_whatsapp_for_call(attempt.id, db)
    return db


def request_json(route):
    return json.loads(route.calls.last.request.content.decode())


def assert_basic_auth(route):
    expected = base64.b64encode(b"test-api-key:test-api-token").decode()
    assert route.calls.last.request.headers["Authorization"] == f"Basic {expected}"


def test_build_exotel_template_payload_matches_documentation_shape():
    payload = build_exotel_template_payload(
        from_number="+919876500001",
        to_number="+919876543210",
        template_name="order_confirmation",
        parameters=[
            {"type": "text", "text": "Rahul"},
            {"type": "text", "text": "#ORD-12345"},
            {"type": "text", "text": "Feb 10, 2024"},
        ],
    )

    message = payload["whatsapp"]["messages"][0]
    assert "custom_data" in payload
    assert message == {
        "from": "+919876500001",
        "to": "+919876543210",
        "content": {
            "recipient_type": "individual",
            "type": "template",
            "template": {
                "name": "order_confirmation",
                "language": {
                    "code": "en",
                    "policy": "deterministic",
                },
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": "Rahul"},
                            {"type": "text", "text": "#ORD-12345"},
                            {"type": "text", "text": "Feb 10, 2024"},
                        ],
                    }
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_call_completion_sends_soil_systems_template_to_lead_phone(monkeypatch, exotel_wa_settings):
    attempt = make_attempt(phone="+919876543210", name="Rahul")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(EXOTEL_URL).mock(return_value=Response(200, json={"message": {"sid": "msg-123"}}))
        db = await run_for_attempt(monkeypatch, attempt)

    payload = request_json(route)
    message = payload["whatsapp"]["messages"][0]
    assert message["from"] == "+918047283246"
    assert message["to"] == "+919876543210"
    assert message["content"]["recipient_type"] == "individual"
    assert message["content"]["type"] == "template"
    assert message["content"]["template"]["name"] == "soil_systems"
    assert message["content"]["template"]["language"] == {"code": "en", "policy": "deterministic"}
    assert message["content"]["template"]["components"] == []
    assert_basic_auth(route)
    assert db.rows[-1].status == WhatsAppLogStatus.sent
    assert db.rows[-1].template_name == "soil_systems"
    assert db.rows[-1].phone == "+919876543210"


@pytest.mark.asyncio
async def test_10_digit_lead_phone_gets_plus_91_prefix(monkeypatch, exotel_wa_settings):
    attempt = make_attempt(phone="9876543210")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(EXOTEL_URL).mock(return_value=Response(200, json={"message": {"sid": "msg-123"}}))
        await run_for_attempt(monkeypatch, attempt)

    assert request_json(route)["whatsapp"]["messages"][0]["to"] == "+919876543210"


@pytest.mark.asyncio
async def test_sender_falls_back_to_legacy_wa_phone_number(monkeypatch, exotel_wa_settings):
    exotel_wa_settings.EXOTEL_WHATSAPP_FROM_NUMBER = None
    exotel_wa_settings.EXOTEL_WHATSAPP_NUMBER = None
    exotel_wa_settings.EXOTEL_WA_PHONE_NUMBER = "+918047283246"
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        route = router.post(EXOTEL_URL).mock(return_value=Response(200, json={"message": {"sid": "msg-123"}}))
        await run_for_attempt(monkeypatch, attempt)

    assert request_json(route)["whatsapp"]["messages"][0]["from"] == "+918047283246"


@pytest.mark.asyncio
async def test_invalid_phone_logs_skipped(monkeypatch, exotel_wa_settings):
    attempt = make_attempt(phone="not-a-phone")
    db = await run_for_attempt(monkeypatch, attempt)

    log = db.rows[-1]
    assert log.status == WhatsAppLogStatus.skipped
    assert log.template_name == "soil_systems"
    assert log.error_message == "invalid or missing phone number"


@pytest.mark.asyncio
async def test_exotel_api_failure_logs_failed(monkeypatch, exotel_wa_settings):
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        router.post(EXOTEL_URL).mock(return_value=Response(500, json={"error": "down"}))
        db = await run_for_attempt(monkeypatch, attempt)

    assert db.rows[-1].status == WhatsAppLogStatus.failed
    assert "Exotel API failed with status 500" in db.rows[-1].error_message


@pytest.mark.asyncio
async def test_exotel_retry_on_failure(monkeypatch, exotel_wa_settings):
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        route = router.post(EXOTEL_URL).mock(
            side_effect=[
                Response(500, json={"error": "down"}),
                Response(200, json={"message": {"sid": "msg-123"}}),
            ]
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert len(route.calls) == 2
    assert db.rows[0].status == WhatsAppLogStatus.failed
    assert db.rows[-1].status == WhatsAppLogStatus.sent


def test_phone_format_helpers():
    assert format_phone_for_exotel_whatsapp("+919876543210") == "+919876543210"
    assert format_phone_for_exotel_whatsapp("9876543210") == "+919876543210"
    assert format_phone_for_exotel_whatsapp("09876543210") == "+919876543210"
    assert format_wati_phone("+919876543210") == "919876543210"
