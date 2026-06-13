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


def _resolver_settings():
    return SimpleNamespace(
        EXOTEL_API_KEY="api-key",
        EXOTEL_API_TOKEN="api-token",
        EXOTEL_ACCOUNT_SID="account-sid",
        EXOTEL_SUBDOMAIN="api.exotel.com",
        EXOTEL_CALLER_ID="08046376848",
        EXOTEL_PHONE_NUMBER="08046376848",
        RETELL_FROM_NUMBER="+918046376848",
        EXOTEL_TIMEZONE="Asia/Kolkata",
    )


# Two overlapping inbound calls to our number: suraj earlier, wexora later.
_INBOUND_CALLS_JSON = {
    "Calls": [
        {"Sid": "wexora-sid", "From": "06361232277", "To": "08046376848", "StartTime": "2026-06-13 10:08:37"},
        {"Sid": "suraj-sid", "From": "09137500132", "To": "08046376848", "StartTime": "2026-06-13 10:01:06"},
        {"Sid": "exophone-leg", "From": "08046376848", "To": "08046376848", "StartTime": "2026-06-13 10:08:40"},
    ]
}


@pytest.fixture
def _no_sleep(monkeypatch):
    """Make the resolver's retry backoff instant in tests."""
    async def _instant(_seconds):
        return None
    monkeypatch.setattr("app.services.exotel_service.asyncio.sleep", _instant)


@pytest.mark.asyncio
async def test_resolver_correlates_by_call_start_time(monkeypatch, _no_sleep):
    """A Retell call starting ~10s after the wexora Exotel leg must resolve to
    wexora — NOT the most-recent-by-list-order nor the earlier suraj call."""
    from datetime import datetime, timezone
    from app.services.exotel_service import fetch_real_inbound_caller_phone

    monkeypatch.setattr("app.services.exotel_service.get_settings", _resolver_settings)

    url = "https://api.exotel.com/v1/Accounts/account-sid/Calls.json"
    # Retell start = wexora Exotel StartTime (04:38:37 UTC) + 10s
    retell_start = datetime(2026, 6, 13, 4, 38, 47, tzinfo=timezone.utc)

    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=url).mock(return_value=Response(200, json=_INBOUND_CALLS_JSON))
        result = await fetch_real_inbound_caller_phone("+918046376848", call_started_at=retell_start)

    assert result == "+916361232277"  # wexora, correctly correlated by time


@pytest.mark.asyncio
async def test_resolver_excludes_own_exophone(monkeypatch, _no_sleep):
    """The resolver must never return our own ExoPhone as the caller."""
    from datetime import datetime, timezone
    from app.services.exotel_service import fetch_real_inbound_caller_phone

    monkeypatch.setattr("app.services.exotel_service.get_settings", _resolver_settings)
    url = "https://api.exotel.com/v1/Accounts/account-sid/Calls.json"
    only_own = {"Calls": [{"Sid": "leg", "From": "08046376848", "To": "08046376848", "StartTime": "2026-06-13 10:08:40"}]}

    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=url).mock(return_value=Response(200, json=only_own))
        result = await fetch_real_inbound_caller_phone(
            "+918046376848", call_started_at=datetime(2026, 6, 13, 4, 38, 47, tzinfo=timezone.utc)
        )

    assert result is None


@pytest.mark.asyncio
async def test_resolver_rejects_far_off_match(monkeypatch, _no_sleep):
    """If no Exotel call is within the sanity window, return None rather than a
    wrong number (the right record may not be listed yet)."""
    from datetime import datetime, timezone
    from app.services.exotel_service import fetch_real_inbound_caller_phone

    monkeypatch.setattr("app.services.exotel_service.get_settings", _resolver_settings)
    url = "https://api.exotel.com/v1/Accounts/account-sid/Calls.json"
    # Retell start is hours away from any listed call.
    retell_start = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)

    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=url).mock(return_value=Response(200, json=_INBOUND_CALLS_JSON))
        result = await fetch_real_inbound_caller_phone("+918046376848", call_started_at=retell_start)

    assert result is None


@pytest.mark.asyncio
async def test_resolver_retries_until_record_appears(monkeypatch, _no_sleep):
    """Exotel's list API lags; the correct call shows up on a later retry."""
    from datetime import datetime, timezone
    from app.services.exotel_service import fetch_real_inbound_caller_phone

    monkeypatch.setattr("app.services.exotel_service.get_settings", _resolver_settings)
    url = "https://api.exotel.com/v1/Accounts/account-sid/Calls.json"
    retell_start = datetime(2026, 6, 13, 4, 49, 59, tzinfo=timezone.utc)

    # First call: only the stale old records (no close match). Second call: the
    # fresh wexora record has propagated in.
    empty = {"Calls": [{"Sid": "old", "From": "09137500132", "To": "08046376848", "StartTime": "2026-06-13 09:42:34"}]}
    ready = {"Calls": [{"Sid": "new", "From": "06361232277", "To": "08046376848", "StartTime": "2026-06-13 10:19:59"}]}

    with respx.mock(assert_all_called=True) as router:
        router.get(url__startswith=url).mock(side_effect=[Response(200, json=empty), Response(200, json=ready)])
        result = await fetch_real_inbound_caller_phone("+918046376848", call_started_at=retell_start)

    assert result == "+916361232277"
