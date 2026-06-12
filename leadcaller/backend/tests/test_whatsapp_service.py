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
EXOTEL_URL = "https://api.in.exotel.com/v2/accounts/test-account-sid/messages"


class FakeDb:
    def __init__(self):
        self.rows = []
        self.commits = 0

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        self.commits += 1

    async def scalar(self, stmt):
        # Mimics the dedup lookup: truthy if a previously-sent log exists
        return any(r.status == WhatsAppLogStatus.sent for r in self.rows) or None


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
        WHATSAPP_ENABLED=True,
        BASE_URL="",
        META_WA_PHONE_NUMBER_ID="test-phone-number-id",
        META_WA_ACCESS_TOKEN="test-access-token",
        EXOTEL_WA_TEMPLATE_SOIL_SYSTEMS=SOIL_SYSTEMS_TEMPLATE,
        # Exotel WA credentials — primary path tries Exotel first
        EXOTEL_WA_API_KEY="test-exotel-key",
        EXOTEL_WA_API_TOKEN="test-exotel-token",
        EXOTEL_WA_SUBDOMAIN="api.in.exotel.com",
        EXOTEL_WA_ACCOUNT_SID="test-account-sid",
        EXOTEL_WA_PHONE_NUMBER="+918047283246",
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
async def test_call_completion_sends_soil_systems_template_via_exotel(monkeypatch, meta_wa_settings):
    """Exotel is the primary path — assert it's called and Meta is not."""
    attempt = make_attempt(phone="+919876543210", name="Rahul")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(EXOTEL_URL).mock(
            return_value=Response(200, json={"response": {"whatsapp": {"messages": [{"status": "success"}]}}})
        )
        db = await run_for_attempt(monkeypatch, attempt)

    payload = request_json(route)
    message = payload["whatsapp"]["messages"][0]
    assert message["to"] == "+919876543210"
    assert message["content"]["template"]["name"] == "soil_systems"
    # The approved template has a document header and NO body variables —
    # the brochure header must be present and no body component sent.
    components = message["content"]["template"]["components"]
    assert components[0]["type"] == "header"
    assert components[0]["parameters"][0]["document"]["link"].endswith(".pdf")
    assert all(c["type"] != "body" for c in components)
    assert db.rows[-1].status == WhatsAppLogStatus.sent
    assert db.rows[-1].template_name == "soil_systems"
    assert db.rows[-1].phone == "+919876543210"


@pytest.mark.asyncio
async def test_10_digit_lead_phone_gets_91_prefix(monkeypatch, meta_wa_settings):
    attempt = make_attempt(phone="9876543210")

    with respx.mock(assert_all_called=True) as router:
        route = router.post(EXOTEL_URL).mock(
            return_value=Response(200, json={"response": {"whatsapp": {"messages": [{"status": "success"}]}}})
        )
        await run_for_attempt(monkeypatch, attempt)

    assert request_json(route)["whatsapp"]["messages"][0]["to"] == "+919876543210"


@pytest.mark.asyncio
async def test_invalid_phone_logs_skipped(monkeypatch, meta_wa_settings):
    attempt = make_attempt(phone="not-a-phone")
    db = await run_for_attempt(monkeypatch, attempt)

    log = db.rows[-1]
    assert log.status == WhatsAppLogStatus.skipped
    assert log.template_name == "soil_systems"
    assert log.error_message == "invalid or missing phone number"


@pytest.mark.asyncio
async def test_exotel_failure_falls_back_to_meta(monkeypatch, meta_wa_settings):
    """When Exotel fails, Meta direct is tried as fallback."""
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        router.post(EXOTEL_URL).mock(return_value=Response(500, json={"error": "exotel down"}))
        router.post(META_URL).mock(return_value=Response(200, json={"message_id": "msg-123"}))
        db = await run_for_attempt(monkeypatch, attempt)

    assert db.rows[-1].status == WhatsAppLogStatus.sent


@pytest.mark.asyncio
async def test_both_providers_failure_logs_failed(monkeypatch, meta_wa_settings):
    """When BOTH Exotel and Meta fail, log as failed with Exotel error (primary)."""
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        router.post(EXOTEL_URL).mock(return_value=Response(500, json={"error": "exotel down"}))
        router.post(META_URL).mock(return_value=Response(500, json={"error": "meta down"}))
        db = await run_for_attempt(monkeypatch, attempt)

    assert db.rows[-1].status == WhatsAppLogStatus.failed
    assert "Exotel WhatsApp failed with status 500" in db.rows[-1].error_message


@pytest.mark.asyncio
async def test_send_retry_on_failure(monkeypatch, meta_wa_settings):
    """When the first attempt fails on both providers, the retry kicks in."""
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        exotel_route = router.post(EXOTEL_URL).mock(
            side_effect=[
                Response(500, json={"error": "exotel down"}),
                Response(200, json={"response": {"whatsapp": {"messages": [{"status": "success"}]}}}),
            ]
        )
        router.post(META_URL).mock(return_value=Response(500, json={"error": "meta down"}))
        db = await run_for_attempt(monkeypatch, attempt)

    assert len(exotel_route.calls) == 2
    assert db.rows[0].status == WhatsAppLogStatus.failed
    assert db.rows[-1].status == WhatsAppLogStatus.sent


@pytest.mark.asyncio
async def test_duplicate_webhook_event_sends_only_once(monkeypatch, meta_wa_settings):
    """Retell fires several webhook events per call — only one WhatsApp send should happen."""
    attempt = make_attempt()

    with respx.mock(assert_all_called=True) as router:
        route = router.post(EXOTEL_URL).mock(
            return_value=Response(200, json={"response": {"whatsapp": {"messages": [{"status": "success"}]}}})
        )
        db = await run_for_attempt(monkeypatch, attempt)
        # Second webhook event for the same attempt — same db with the sent log present
        await run_for_attempt(monkeypatch, attempt, db=db)

    assert len(route.calls) == 1
    sent_logs = [r for r in db.rows if r.status == WhatsAppLogStatus.sent]
    assert len(sent_logs) == 1


def test_phone_format_helpers():
    assert format_phone_for_whatsapp("+919876543210") == "+919876543210"
    assert format_phone_for_whatsapp("9876543210") == "+919876543210"
    assert format_phone_for_whatsapp("09876543210") == "+919876543210"
    assert format_wati_phone("+919876543210") == "919876543210"
