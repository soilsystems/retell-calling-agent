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
    build_meta_template_payload,
    format_phone_for_whatsapp,
    format_wati_phone,
    send_whatsapp_for_call,
)

META_URL = "https://graph.facebook.com/v17.0/test-phone-number-id/messages"


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
def meta_wa_settings(monkeypatch):
    settings = SimpleNamespace(
        META_WA_PHONE_NUMBER_ID="test-phone-number-id",
        META_WA_ACCESS_TOKEN="test-access-token",
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


def assert_bearer_auth(route):
    assert route.calls.last.request.headers["Authorization"] == "Bearer test-access-token"


def test_build_meta_template_payload_matches_shape():
    payload = build_meta_template_payload(
        to_number="919876543210",
        template_name="order_confirmation",
        components=[
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": "Rahul"},
                    {"type": "text", "text": "#ORD-12345"},
                ],
            }
        ],
    )

    assert payload == {
        "messaging_product": "whatsapp",
        "to": "919876543210",
        "type": "template",
        "template": {
            "name": "order_confirmation",
            "language": {
                "code": "en",
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Rahul"},
                        {"type": "text", "text": "#ORD-12345"},
                    ],
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_call_completion_sends_soil_systems_template_to_lead_phone(monkeypatch, meta_wa_settings):
    attempt = make_attempt(phone="+919876543210", name="Rahul")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(META_URL).mock(return_value=Response(200, json={"message_id": "msg-123"}))
        db = await run_for_attempt(monkeypatch, attempt)

    payload = request_json(route)
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == "919876543210"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "soil_systems"
    assert payload["template"]["language"]["code"] == "en"
    assert "components" not in payload["template"]
    assert_bearer_auth(route)
    assert db.rows[-1].status == WhatsAppLogStatus.sent
    assert db.rows[-1].template_name == "soil_systems"
    assert db.rows[-1].phone == "+919876543210"


@pytest.mark.asyncio
async def test_10_digit_lead_phone_gets_91_prefix(monkeypatch, meta_wa_settings):
    attempt = make_attempt(phone="9876543210")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(META_URL).mock(return_value=Response(200, json={"message_id": "msg-123"}))
        await run_for_attempt(monkeypatch, attempt)

    assert request_json(route)["to"] == "919876543210"


@pytest.mark.asyncio
async def test_invalid_phone_logs_skipped(monkeypatch, meta_wa_settings):
    attempt = make_attempt(phone="not-a-phone")
    db = await run_for_attempt(monkeypatch, attempt)

    log = db.rows[-1]
    assert log.status == WhatsAppLogStatus.skipped
    assert log.template_name == "soil_systems"
    assert log.error_message == "invalid or missing phone number"


@pytest.mark.asyncio
async def test_meta_api_failure_logs_failed(monkeypatch, meta_wa_settings):
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        router.post(META_URL).mock(return_value=Response(500, json={"error": "down"}))
        db = await run_for_attempt(monkeypatch, attempt)

    assert db.rows[-1].status == WhatsAppLogStatus.failed
    assert "Meta Cloud API failed with status 500" in db.rows[-1].error_message


@pytest.mark.asyncio
async def test_meta_retry_on_failure(monkeypatch, meta_wa_settings):
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        route = router.post(META_URL).mock(
            side_effect=[
                Response(500, json={"error": "down"}),
                Response(200, json={"message_id": "msg-123"}),
            ]
        )
        db = await run_for_attempt(monkeypatch, attempt)

    assert len(route.calls) == 2
    assert db.rows[0].status == WhatsAppLogStatus.failed
    assert db.rows[-1].status == WhatsAppLogStatus.sent


def test_phone_format_helpers():
    assert format_phone_for_whatsapp("+919876543210") == "+919876543210"
    assert format_phone_for_whatsapp("9876543210") == "+919876543210"
    assert format_phone_for_whatsapp("09876543210") == "+919876543210"
    assert format_wati_phone("+919876543210") == "919876543210"
