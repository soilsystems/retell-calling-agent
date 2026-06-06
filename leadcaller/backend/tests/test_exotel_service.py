from types import SimpleNamespace
import json
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
import respx
from fastapi import HTTPException
from httpx import Response

from app.models import LanguagePreference, Lead
from app.services.exotel_service import connect_exotel_call
from app.services.exotel_service import _required_setting


class FakeDb:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_connect_exotel_call_posts_expected_form(monkeypatch):
    settings = SimpleNamespace(
        EXOTEL_ACCOUNT_SID="account-sid",
        EXOTEL_API_KEY="api-key",
        EXOTEL_API_TOKEN="api-token",
        EXOTEL_SUBDOMAIN="api.in.exotel.com",
        EXOTEL_CALLER_ID="08000000000",
        EXOTEL_EXOML_URL="http://my.exotel.com/account-sid/exoml/start_voice/app-id",
        EXOTEL_STATUS_CALLBACK="https://example.com/webhooks/exotel/status",
        EXOTEL_CALL_TYPE="trans",
        BASE_URL="https://example.com",
    )
    monkeypatch.setattr("app.services.exotel_service.get_settings", lambda: settings)

    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi",
        phone="+919876543210",
        city="Bengaluru",
        campaign="May Campaign",
        language_preference=LanguagePreference.english,
    )
    db = FakeDb()

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.in.exotel.com/v1/Accounts/account-sid/Calls/connect").mock(
            return_value=Response(200, json={"Call": {"Sid": "call-sid"}})
        )
        result = await connect_exotel_call(lead, db)

    request = route.calls.last.request
    body = request.content.decode()
    form = parse_qs(body)
    assert form["From"] == ["+919876543210"]
    assert form["CallerId"] == ["08000000000"]
    assert form["Url"] == ["http://my.exotel.com/account-sid/exoml/start_voice/app-id"]
    assert form["CallType"] == ["trans"]
    custom_field = json.loads(form["CustomField"][0])
    assert custom_field["lead_id"] == str(lead.id)
    assert custom_field["lead_name"] == "Ravi"
    assert custom_field["lead_phone"] == "+919876543210"
    assert result["status"] == "queued"
    assert result["mode"] == "exotel"
    assert db.rows[-1].operation == "exotel_connect_call"
    assert db.rows[-1].success is True


@pytest.mark.asyncio
async def test_connect_exotel_call_uses_phone_number_as_caller_id_fallback(monkeypatch):
    settings = SimpleNamespace(
        EXOTEL_ACCOUNT_SID="account-sid",
        EXOTEL_API_KEY="api-key",
        EXOTEL_API_TOKEN="api-token",
        EXOTEL_SUBDOMAIN="api.in.exotel.com",
        EXOTEL_CALLER_ID=None,
        EXOTEL_PHONE_NUMBER="+918046376848",
        EXOTEL_EXOML_URL="http://my.exotel.com/account-sid/exoml/start_voice/app-id",
        EXOTEL_STATUS_CALLBACK="https://example.com/webhooks/exotel/status",
        EXOTEL_CALL_TYPE="trans",
        BASE_URL="https://example.com",
    )
    monkeypatch.setattr("app.services.exotel_service.get_settings", lambda: settings)

    lead = Lead(
        id=uuid4(),
        zoho_lead_id="zoho-1",
        name="Ravi",
        phone="+919876543210",
        city="Bengaluru",
        campaign="May Campaign",
        language_preference=LanguagePreference.english,
    )
    db = FakeDb()

    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.in.exotel.com/v1/Accounts/account-sid/Calls/connect").mock(
            return_value=Response(200, json={"Call": {"Sid": "call-sid"}})
        )
        await connect_exotel_call(lead, db)

    body = route.calls.last.request.content.decode()
    assert "CallerId=%2B918046376848" in body


def test_required_setting_rejects_placeholder_values():
    with pytest.raises(HTTPException) as exc:
        _required_setting("EXOTEL_CALLER_ID", "0XXXXXX4890")

    assert exc.value.status_code == 500
    assert exc.value.detail == "EXOTEL_CALLER_ID still has a placeholder value"


def test_format_phone_number():
    from app.services.exotel_service import format_phone_number
    assert format_phone_number("+919876543210") == "+919876543210"
    assert format_phone_number("09876543210") == "+919876543210"
    assert format_phone_number("9876543210") == "+919876543210"
    assert format_phone_number("+91 98765-43210") == "+919876543210"
    assert format_phone_number("+9109876543210") == "+919876543210"
    assert format_phone_number("  98765  43210  ") == "+919876543210"
