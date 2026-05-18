import logging
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CrmSyncLog, Lead

logger = logging.getLogger(__name__)


def _required_setting(name: str, value: str | None) -> str:
    if not value or value.strip() == "":
        raise HTTPException(status_code=500, detail=f"{name} is not configured")
    value = value.strip()
    if "replace-with" in value or "<" in value or ">" in value or "XXXX" in value:
        raise HTTPException(status_code=500, detail=f"{name} still has a placeholder value")
    return value


def _parse_exotel_response(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"response": data}
    except ValueError:
        pass

    text = response.text.strip()
    if not text:
        return {}

    try:
        root = ElementTree.fromstring(text)
        return {
            child.tag.rsplit("}", 1)[-1]: child.text
            for child in root.iter()
            if child is not root and child.text
        }
    except ElementTree.ParseError:
        return {"raw_response": text}


async def connect_exotel_call(lead: Lead, db: AsyncSession) -> dict[str, Any]:
    """Ask Exotel to bridge a call to the lead using the configured ExoML app."""
    settings = get_settings()
    account_sid = _required_setting("EXOTEL_ACCOUNT_SID", settings.EXOTEL_ACCOUNT_SID)
    api_key = _required_setting("EXOTEL_API_KEY", settings.EXOTEL_API_KEY)
    api_token = _required_setting("EXOTEL_API_TOKEN", settings.EXOTEL_API_TOKEN)
    subdomain = _required_setting("EXOTEL_SUBDOMAIN", settings.EXOTEL_SUBDOMAIN)
    caller_id = _required_setting("EXOTEL_CALLER_ID or EXOTEL_PHONE_NUMBER", settings.EXOTEL_CALLER_ID or settings.EXOTEL_PHONE_NUMBER)
    exoml_url = _required_setting("EXOTEL_EXOML_URL", settings.EXOTEL_EXOML_URL)
    status_callback = (
        settings.EXOTEL_STATUS_CALLBACK
        or f"{settings.BASE_URL.rstrip('/')}/webhooks/exotel/status"
    )

    payload = {
        "From": lead.phone,
        "CallerId": caller_id,
        "Url": exoml_url,
        "CallType": settings.EXOTEL_CALL_TYPE,
        "StatusCallback": status_callback,
    }
    url = f"https://{subdomain.rstrip('/')}/v1/Accounts/{account_sid}/Calls/connect"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                auth=(api_key, api_token),
                data=payload,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            response_data = _parse_exotel_response(response)
    except httpx.HTTPStatusError as exc:
        error = exc.response.text[:1000]
        logger.warning("Exotel connect call failed for lead=%s status=%s", lead.id, exc.response.status_code)
        db.add(
            CrmSyncLog(
                lead_id=lead.id,
                operation="exotel_connect_call",
                success=False,
                error_message=error,
                synced_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Exotel call failed: {error}") from exc
    except httpx.HTTPError as exc:
        logger.warning("Exotel connect call request failed for lead=%s: %s", lead.id, exc)
        db.add(
            CrmSyncLog(
                lead_id=lead.id,
                operation="exotel_connect_call",
                success=False,
                error_message=str(exc),
                synced_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Exotel call failed: {exc}") from exc

    db.add(
        CrmSyncLog(
            lead_id=lead.id,
            operation="exotel_connect_call",
            success=True,
            error_message=None,
            synced_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    return {
        "mode": "exotel",
        "status": "queued",
        "lead_name": lead.name,
        "phone": lead.phone,
        "provider_response": response_data,
    }
