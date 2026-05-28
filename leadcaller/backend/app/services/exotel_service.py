import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
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


def format_phone_number(phone: str) -> str:
    """Format a phone number to strict E.164 format for India (+91XXXXXXXXXX)."""
    # Remove all whitespace, dashes, parens, and any formatting characters
    cleaned = "".join(c for c in phone if c.isdigit() or c == "+")
    
    # Handle already +91
    if cleaned.startswith("+91"):
        digits = cleaned[3:]
        # Strip any leading zeros after +91
        while digits.startswith("0"):
            digits = digits[1:]
        return f"+91{digits}"
        
    # Handle leading 91 (without +)
    if cleaned.startswith("91") and len(cleaned) == 12:
        return f"+{cleaned}"
        
    # Handle leading 0
    if cleaned.startswith("0"):
        digits = cleaned[1:]
        if len(digits) == 10:
            return f"+91{digits}"
            
    # Handle 10 digits
    if len(cleaned) == 10 and cleaned.isdigit():
        return f"+91{cleaned}"
        
    # Fallback to E.164 if it already starts with +
    if cleaned.startswith("+"):
        return cleaned
        
    # Default to prepending +91 for 10-digit mobile numbers
    if len(cleaned) > 0:
        return f"+91{cleaned}"
        
    return cleaned


async def connect_exotel_call(lead: Lead, db: AsyncSession) -> dict[str, Any]:
    """Initiate an outbound Exotel call to the lead.

    Exotel /Calls/connect flow:
      - Exotel calls `From` (lead's phone number).
      - The lead sees `CallerId` (your ExoPhone virtual number) on their screen.
      - When the lead picks up, Exotel executes the ExoML app at `Url`,
        which handles connecting the agent (via SIP / phone / IVR).
    """
    settings = get_settings()
    account_sid = _required_setting("EXOTEL_ACCOUNT_SID", settings.EXOTEL_ACCOUNT_SID)
    api_key = _required_setting("EXOTEL_API_KEY", settings.EXOTEL_API_KEY)
    api_token = _required_setting("EXOTEL_API_TOKEN", settings.EXOTEL_API_TOKEN)
    subdomain = _required_setting("EXOTEL_SUBDOMAIN", settings.EXOTEL_SUBDOMAIN)
    caller_id = _required_setting(
        "EXOTEL_CALLER_ID or EXOTEL_PHONE_NUMBER",
        settings.EXOTEL_CALLER_ID or settings.EXOTEL_PHONE_NUMBER,
    )
    exoml_url = _required_setting("EXOTEL_EXOML_URL", settings.EXOTEL_EXOML_URL)
    status_callback = (
        settings.EXOTEL_STATUS_CALLBACK
        or f"{settings.BASE_URL.rstrip('/')}/webhooks/exotel/status"
    )

    payload: dict[str, str] = {
        "From": format_phone_number(lead.phone),       # Lead's number — Exotel calls this
        "CallerId": caller_id,                         # Your ExoPhone — shown to the lead
        "Url": exoml_url,                              # ExoML app — runs when lead picks up
        "CallType": settings.EXOTEL_CALL_TYPE,
        "StatusCallback": status_callback,
        "CustomField": lead.name,
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


async def connect_exotel_human_call(lead: Lead, agent_phone: str, db: AsyncSession) -> dict[str, Any]:
    """Initiate a direct Exotel bridge call between a human agent and a lead.

    Flow:
      1. Exotel calls the agent (From=agent_phone) — Leg1.
      2. Agent picks up.
      3. Exotel calls the lead (To=lead_phone) — Leg2.
      4. Both parties are bridged. Lead does not wait.

    Note: Leg2 one-way audio is usually a carrier network NAT/routing issue.
    Forcing `Record=true` helps proxy media through Exotel to mitigate this.
    """
    settings = get_settings()
    account_sid = _required_setting("EXOTEL_ACCOUNT_SID", settings.EXOTEL_ACCOUNT_SID)
    api_key = _required_setting("EXOTEL_API_KEY", settings.EXOTEL_API_KEY)
    api_token = _required_setting("EXOTEL_API_TOKEN", settings.EXOTEL_API_TOKEN)
    subdomain = _required_setting("EXOTEL_SUBDOMAIN", settings.EXOTEL_SUBDOMAIN)
    caller_id = _required_setting(
        "EXOTEL_CALLER_ID or EXOTEL_PHONE_NUMBER",
        settings.EXOTEL_CALLER_ID or settings.EXOTEL_PHONE_NUMBER,
    )
    base_url = _required_setting("BASE_URL", settings.BASE_URL)
    status_callback = (
        settings.EXOTEL_STATUS_CALLBACK
        or f"{base_url.rstrip('/')}/webhooks/exotel/status"
    )

    import json
    
    payload: dict[str, str] = {
        # Agent is called first (Leg1). When they answer, Exotel dials the lead (Leg2).
        "From": format_phone_number(agent_phone),
        "To": format_phone_number(lead.phone),
        "CallerId": caller_id,
        "CallType": settings.EXOTEL_CALL_TYPE,
        "Record": "true",
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
        logger.warning("Exotel human bridge call failed for lead=%s status=%s", lead.id, exc.response.status_code)
        db.add(
            CrmSyncLog(
                lead_id=lead.id,
                operation="exotel_human_bridge",
                success=False,
                error_message=error,
                synced_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Exotel bridge call failed: {error}") from exc
    except httpx.HTTPError as exc:
        logger.warning("Exotel human bridge request failed for lead=%s: %s", lead.id, exc)
        db.add(
            CrmSyncLog(
                lead_id=lead.id,
                operation="exotel_human_bridge",
                success=False,
                error_message=str(exc),
                synced_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Exotel bridge call failed: {exc}") from exc

    db.add(
        CrmSyncLog(
            lead_id=lead.id,
            operation="exotel_human_bridge",
            success=True,
            error_message=None,
            synced_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    return {
        "mode": "exotel_human",
        "status": "queued",
        "lead_name": lead.name,
        "phone": lead.phone,
        "agent_phone": agent_phone,
        "provider_response": response_data,
    }

