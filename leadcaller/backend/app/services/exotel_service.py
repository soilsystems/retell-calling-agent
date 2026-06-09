import logging
import json
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

# ── In-memory cache for pending outbound bridge calls ──
# Populated by connect_exotel_call_with_retell_ai(), consumed by the Retell
# inbound webhook handler so it can respond instantly without DB roundtrips.
# Dict of {lead_phone_suffix: {lead_name, lead_phone, lead_id, city, campaign, ...}}
_pending_outbound_bridges: dict[str, dict] = {}


def cache_outbound_bridge(lead: "Lead") -> None:
    """Cache lead info for fast lookup in the Retell inbound webhook."""
    from datetime import datetime, timezone
    suffix = "".join(c for c in (lead.phone or "") if c.isdigit())[-10:]
    if not suffix:
        return
    _pending_outbound_bridges[suffix] = {
        "lead_id": str(lead.id),
        "lead_name": lead.name,
        "lead_phone": lead.phone,
        "city": lead.city or "",
        "campaign": lead.campaign or "",
        "zoho_lead_id": lead.zoho_lead_id,
        "cached_at": datetime.now(timezone.utc).timestamp(),
    }
    # Evict stale entries (older than 5 minutes)
    cutoff = datetime.now(timezone.utc).timestamp() - 300
    stale = [k for k, v in _pending_outbound_bridges.items() if v["cached_at"] < cutoff]
    for k in stale:
        _pending_outbound_bridges.pop(k, None)
    logger.info("[OutboundCache] Cached lead=%s phone_suffix=%s", lead.name, suffix)


def pop_pending_outbound_bridge() -> dict | None:
    """Pop the most recent pending outbound bridge (LIFO). Returns None if empty."""
    if not _pending_outbound_bridges:
        return None
    # Return and remove the most recently cached entry
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).timestamp() - 300
    # Find the newest non-stale entry
    newest_key = None
    newest_ts = 0.0
    for k, v in list(_pending_outbound_bridges.items()):
        if v["cached_at"] < cutoff:
            _pending_outbound_bridges.pop(k, None)
            continue
        if v["cached_at"] > newest_ts:
            newest_ts = v["cached_at"]
            newest_key = k
    if newest_key is None:
        return None
    return _pending_outbound_bridges.pop(newest_key)


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


def _extract_exotel_call_sid(data: dict[str, Any]) -> str | None:
    candidates: list[Any] = [
        data.get("Sid"),
        data.get("CallSid"),
        data.get("CallUUID"),
        data.get("CallUuid"),
    ]
    call_data = data.get("Call")
    if isinstance(call_data, dict):
        candidates.extend(
            [
                call_data.get("Sid"),
                call_data.get("CallSid"),
                call_data.get("CallUUID"),
                call_data.get("CallUuid"),
            ]
        )

    for candidate in candidates:
        if candidate:
            return str(candidate)
    return None


def _provider_log_payload(response_data: dict[str, Any]) -> str:
    return json.dumps(
        {
            "provider": "exotel",
            "call_sid": _extract_exotel_call_sid(response_data),
            "response": response_data,
        },
        default=str,
    )[:32000]


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


async def _register_retell_phone_call(lead: "Lead", settings: Any) -> str:
    """Pre-register a Retell call session and return the call_id.

    The call_id is used as the WebSocket access token:
      wss://api.retellai.com/audio-websocket/{call_id}
    This must be called BEFORE Exotel dials the lead so the session is ready
    when the lead picks up and ExoML opens the WebSocket.
    """
    from app.services.retell_service import LANGUAGE_ADAPTATION_INSTRUCTION  # avoid circular at import time

    clean_name = lead.name.replace("(Sample)", "").replace("(sample)", "").replace("Test", "").strip()
    outbound_script = (
        "Outbound callback/sales call. Start by confirming the lead is available, "
        "then remind them they had enquired about Soil Systems land investment. "
        "Do not thank them for calling. Ask whether they want details, a brochure, "
        "or a site visit. "
        f"{LANGUAGE_ADAPTATION_INSTRUCTION}"
    )
    begin_message = (
        f"Hello, am I speaking with {clean_name}? "
        "This is Vikas calling from Soil Systems about your land investment enquiry."
    )
    variables = {
        "lead_name": clean_name,
        "customer_name": clean_name,
        "name": clean_name,
        "agent_name": "Vikas",
        "language": "auto",
        "language_preference": "auto",
        "language_instruction": LANGUAGE_ADAPTATION_INSTRUCTION,
        "city": lead.city or "",
        "campaign": lead.campaign or "",
        "zoho_lead_id": lead.zoho_lead_id,
        "call_direction": "outbound",
        "inbound_call": "false",
        "outbound_bridge_call": "true",
        "call_context": "outbound",
        "call_script": outbound_script,
        "conversation_script": outbound_script,
        "opening_instruction": "You placed this outbound callback call to the lead.",
    }
    body: dict[str, Any] = {
        "agent_id": settings.RETELL_AGENT_ID,
        "retell_llm_dynamic_variables": variables,
        "agent_override": {
            "retell_llm": {
                "begin_message": begin_message,
                "general_prompt": outbound_script,
            },
            "conversation_flow": {
                "begin_message": begin_message,
                "global_prompt": outbound_script,
            },
        },
        "metadata": {
            "lead_id": str(lead.id),
            "lead_name": clean_name,
            "lead_phone": lead.phone,
        },
        "webhook_url": f"{settings.BASE_URL.rstrip('/')}/webhooks/retell/call-completed",
    }
    if settings.RETELL_AGENT_VERSION is not None:
        body["agent_version"] = settings.RETELL_AGENT_VERSION

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            "https://api.retellai.com/v2/register-phone-call",
            headers={"Authorization": f"Bearer {settings.RETELL_API_KEY}"},
            json=body,
        )
        r.raise_for_status()
        data = r.json()

    call_id = data.get("call_id")
    if not call_id:
        raise RuntimeError(f"Retell register-phone-call returned no call_id: {data}")
    logger.info("[Retell] Registered phone call session call_id=%s for lead=%s", call_id, lead.id)
    return call_id


async def connect_exotel_call_with_retell_ai(lead: "Lead", db: AsyncSession) -> dict[str, Any]:
    """Dial the lead via Exotel REST API and bridge to the Retell AI agent.

    Flow:
      1. Exotel calls the lead (From=lead.phone).
      2. When the lead picks up, Exotel fetches the ExoML from our server.
      3. ExoML returns <Dial>{retell_sip_number}</Dial>.
      4. Exotel dials +918046376848 (Retell SIP trunk), which routes as an
         inbound call through Exotel SIP → Retell.
      5. Retell's /webhooks/retell/inbound handler detects a recent
         'exotel_connect_call' log for this lead and uses the outbound-bridge
         AI script so the agent speaks first.

    This reuses the proven inbound path instead of the broken SIP outbound path.
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

    # Use Exotel's native two-leg bridge: Exotel calls the lead (Leg1),
    # when the lead picks up, Exotel calls the Retell SIP number (Leg2).
    # Retell receives this as an inbound call and the /retell/inbound handler
    # detects the recent 'exotel_connect_call' log → uses outbound AI script.
    retell_sip_number = _required_setting("RETELL_FROM_NUMBER", settings.RETELL_FROM_NUMBER)

    # Cache lead info so the Retell inbound webhook can respond instantly
    # without slow DB roundtrips to Supabase.
    cache_outbound_bridge(lead)

    payload: dict[str, str] = {
        "From": format_phone_number(lead.phone),       # Lead's number — Exotel calls this first (Leg1)
        "To": retell_sip_number,                        # Retell SIP — Exotel calls this when lead picks up (Leg2)
        "CallerId": caller_id,                          # ExoPhone — shown to the lead as caller ID
        "CallType": settings.EXOTEL_CALL_TYPE,
        "StatusCallback": status_callback,
        "Record": "true",                               # Force media proxy through Exotel for reliable audio
        "CustomField": json.dumps(
            {
                "lead_id": str(lead.id),
                "lead_name": lead.name,
                "lead_phone": format_phone_number(lead.phone),
            }
        ),
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
        logger.warning("Exotel AI bridge call failed for lead=%s status=%s", lead.id, exc.response.status_code)
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
        logger.warning("Exotel AI bridge request failed for lead=%s: %s", lead.id, exc)
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

    # Log "exotel_connect_call" so _is_recent_exotel_outbound_for_lead() in the
    # Retell inbound webhook can detect this is an outbound bridge and use the
    # right AI script.
    db.add(
        CrmSyncLog(
            lead_id=lead.id,
            operation="exotel_connect_call",
            success=True,
            error_message=_provider_log_payload(response_data),
            synced_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()

    return {
        "mode": "ai",
        "status": "queued",
        "lead_name": lead.name,
        "phone": lead.phone,
    }


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
        "CustomField": json.dumps(
            {
                "lead_id": str(lead.id),
                "lead_name": lead.name,
                "lead_phone": format_phone_number(lead.phone),
            }
        ),
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
            error_message=_provider_log_payload(response_data),
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
        "CustomField": json.dumps(
            {
                "lead_id": str(lead.id),
                "lead_name": lead.name,
                "lead_phone": format_phone_number(lead.phone),
            }
        ),
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
