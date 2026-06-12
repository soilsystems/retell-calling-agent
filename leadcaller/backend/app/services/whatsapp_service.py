import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import CallAttempt, CallJob, Lead, WhatsAppLog, WhatsAppLogStatus

logger = logging.getLogger(__name__)

SOIL_SYSTEMS_TEMPLATE = "soil_systems"

# In-memory set of call_attempt ids that have claimed a WhatsApp send this
# process lifetime. Prevents duplicate sends when Retell fires several webhook
# events for the same call near-simultaneously. See send_whatsapp_for_call.
_whatsapp_claims: set[str] = set()

# The approved soil_systems template has a DOCUMENT header and a body with NO
# variables. The header document must be supplied on every send; body params
# must NOT be sent (Meta silently drops the message on component mismatch).
BROCHURE_URL = "https://www.soilsystems.in/_files/ugd/6c151e_1f49d9ce4c1242cdbc5550f67ca0d18d.pdf"
BROCHURE_FILENAME = "Woods-and-Spices.pdf"


def _header_components(template_name: str) -> list[dict[str, Any]]:
    """Components required by the template definition beyond body params."""
    if template_name == SOIL_SYSTEMS_TEMPLATE:
        return [{
            "type": "header",
            "parameters": [{
                "type": "document",
                "document": {"link": BROCHURE_URL, "filename": BROCHURE_FILENAME},
            }],
        }]
    return []


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_name(name: str) -> str:
    return name.replace("(Sample)", "").replace("(sample)", "").replace("Test", "").strip() or name.strip()


def format_phone_for_whatsapp(phone: str | None) -> str | None:
    """Normalize phone to E.164 with leading +, e.g. +919137500132"""
    if not phone:
        return None
    digits = "".join(char for char in phone if char.isdigit())
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+91{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    return phone.strip() if phone.strip().startswith("+") else None


# Keep old name as alias so existing callers don't break
format_phone_for_exotel_whatsapp = format_phone_for_whatsapp


def format_meta_phone(phone: str | None) -> str | None:
    """Return digits-only format required by Meta Cloud API, e.g. 919137500132"""
    formatted = format_phone_for_whatsapp(phone)
    return formatted.lstrip("+") if formatted else None


# Kept for any legacy references
def format_wati_phone(phone: str | None) -> str | None:
    return format_meta_phone(phone)


def _template_name() -> str:
    settings = get_settings()
    return settings.EXOTEL_WA_TEMPLATE_SOIL_SYSTEMS or SOIL_SYSTEMS_TEMPLATE


def _meta_credentials() -> tuple[str, str]:
    """Return (phone_number_id, access_token) from settings."""
    settings = get_settings()
    phone_number_id = settings.META_WA_PHONE_NUMBER_ID or ""
    access_token = settings.META_WA_ACCESS_TOKEN or ""
    return phone_number_id, access_token


def _meta_url(phone_number_id: str) -> str:
    return f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"


def build_meta_template_payload(
    *,
    to_number: str,
    template_name: str,
    language_code: str = "en",
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components
    return payload


async def _load_attempt(call_attempt_id: uuid.UUID, db: AsyncSession) -> CallAttempt | None:
    result = await db.execute(
        select(CallAttempt)
        .options(selectinload(CallAttempt.call_job).selectinload(CallJob.lead))
        .where(CallAttempt.id == call_attempt_id)
    )
    return result.scalar_one_or_none()


async def log_whatsapp(
    db: AsyncSession,
    *,
    lead_id: uuid.UUID,
    call_attempt_id: uuid.UUID,
    phone: str | None,
    template_name: str | None,
    status: WhatsAppLogStatus,
    response_body: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> WhatsAppLog:
    log = WhatsAppLog(
        lead_id=lead_id,
        call_attempt_id=call_attempt_id,
        phone=phone,
        template_name=template_name,
        status=status,
        wati_response=response_body,
        error_message=error_message,
        sent_at=_utcnow(),
    )
    db.add(log)
    await db.commit()
    return log


def _response_body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"text": response.text}
    return payload if isinstance(payload, dict) else {"body": payload}


async def _send_whatsapp_exotel(
    lead_phone: str,
    template_name: str,
    parameters: list[dict[str, str]] | None,
) -> dict[str, Any]:
    """Send WhatsApp via Exotel's WhatsApp Business API (primary path)."""
    settings = get_settings()
    api_key = settings.EXOTEL_WA_API_KEY or ""
    api_token = settings.EXOTEL_WA_API_TOKEN or ""
    subdomain = settings.EXOTEL_WA_SUBDOMAIN or "api.in.exotel.com"
    account_sid = settings.EXOTEL_WA_ACCOUNT_SID or ""
    from_raw = settings.EXOTEL_WA_PHONE_NUMBER or ""

    if not all([api_key, api_token, account_sid, from_raw]):
        raise RuntimeError(
            "EXOTEL_WA_API_KEY, EXOTEL_WA_API_TOKEN, EXOTEL_WA_ACCOUNT_SID, "
            "and EXOTEL_WA_PHONE_NUMBER must be set in .env"
        )

    from_number = format_phone_for_whatsapp(from_raw)
    to_number = format_phone_for_whatsapp(lead_phone)
    if not from_number or not to_number:
        raise RuntimeError(f"Invalid phone for Exotel WhatsApp: from={from_raw} to={lead_phone}")

    # Header components required by the template, then body params if any.
    # NOTE: component shape must exactly match the approved template — extra
    # or missing components make Meta drop the message silently after the 202.
    components: list[dict[str, Any]] = _header_components(template_name)
    if parameters:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p.get("value", "") if isinstance(p, dict) else str(p)} for p in parameters],
        })

    # Include a status callback so Exotel notifies us of delivery failures.
    base_url = (settings.BASE_URL or "").rstrip("/")
    status_callback = f"{base_url}/webhooks/whatsapp/status" if base_url else None

    message_body: dict[str, Any] = {
        "from": from_number,
        "to": to_number,
        "content": {
            "recipient_type": "individual",
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "en", "policy": "deterministic"},
                "components": components,
            },
        },
    }
    if status_callback:
        message_body["status_callback"] = status_callback
        message_body["callback_events"] = ["sent", "delivered", "read", "failed"]

    payload = {
        "custom_data": str(uuid.uuid4()),
        "whatsapp": {"messages": [message_body]},
    }

    url = f"https://{subdomain}/v2/accounts/{account_sid}/messages"
    logger.info("[WhatsApp/Exotel] POST %s to=%s template=%s", url, to_number, template_name)

    async with httpx.AsyncClient(auth=(api_key, api_token), timeout=15.0) as client:
        response = await client.post(url, headers={"Content-Type": "application/json"}, json=payload)

    body = _response_body(response)
    logger.info("[WhatsApp/Exotel] response=%s %s", response.status_code, body)
    if response.status_code >= 400:
        raise RuntimeError(f"Exotel WhatsApp failed with status {response.status_code}: {body}")
    return body


async def _send_whatsapp_meta(
    lead_phone: str,
    template_name: str,
    parameters: list[dict[str, str]] | None,
) -> dict[str, Any]:
    """Send WhatsApp directly via Meta Cloud API (fallback path).

    Currently blocked until the phone number is deregistered from Exotel's
    Cloud API instance — kept as fallback for when migration completes.
    """
    phone_number_id, access_token = _meta_credentials()
    if not phone_number_id or not access_token:
        raise RuntimeError(
            "META_WA_PHONE_NUMBER_ID and META_WA_ACCESS_TOKEN must be set in .env"
        )

    to_number = format_meta_phone(lead_phone)
    if not to_number:
        raise RuntimeError(f"Invalid lead phone for WhatsApp: {lead_phone}")

    components: list[dict[str, Any]] = _header_components(template_name)
    if parameters:
        components.append({"type": "body", "parameters": parameters})

    payload = build_meta_template_payload(
        to_number=to_number,
        template_name=template_name,
        components=components or None,
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    logger.info("[WhatsApp/Meta] payload=%s", json.dumps(payload, indent=2))

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            _meta_url(phone_number_id),
            json=payload,
            headers=headers,
        )

    logger.info("[WhatsApp/Meta] response=%s %s", response.status_code, response.text)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Meta Cloud API failed with status {response.status_code}: {_response_body(response)}"
        )
    return _response_body(response)


async def send_whatsapp(
    lead_phone: str,
    template_name: str,
    parameters: list[dict[str, str]] | None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """Send WhatsApp message — Exotel primary, Meta direct as fallback.

    Exotel is primary because the phone number +91 80 4728 3246 is provisioned
    by Exotel and registered to their Cloud API app. Direct Meta send will fail
    until the number is deregistered from Exotel.
    """
    try:
        return await _send_whatsapp_exotel(lead_phone, template_name, parameters)
    except Exception as exotel_exc:
        logger.warning("[WhatsApp] Exotel send failed, trying Meta direct fallback: %s", exotel_exc)
        try:
            return await _send_whatsapp_meta(lead_phone, template_name, parameters)
        except Exception as meta_exc:
            logger.error("[WhatsApp] Both Exotel and Meta send failed. Exotel: %s | Meta: %s", exotel_exc, meta_exc)
            # Re-raise the Exotel error since that's the primary path
            raise exotel_exc


async def send_whatsapp_for_call(
    call_attempt_id: uuid.UUID,
    db: AsyncSession | None = None,
    retry_once: bool = True,
) -> None:
    if db is None:
        async with AsyncSessionLocal() as session:
            await send_whatsapp_for_call(call_attempt_id, session, retry_once=retry_once)
        return

    attempt = await _load_attempt(call_attempt_id, db)
    if not attempt:
        logger.warning("[WhatsApp] call_attempt_id=%s not found", call_attempt_id)
        return

    lead: Lead = attempt.call_job.lead
    template_name = _template_name()

    # Dedup guard: Retell fires several webhook events per call (call_started,
    # call_ended, call_analyzed), each queuing this task — sometimes within the
    # same second. Two guards together prevent duplicate sends:
    #   1. In-memory claim set — race-safe within this process. asyncio is
    #      single-threaded so the membership check + add below is atomic (no
    #      await between them), so only ONE concurrent task wins the claim.
    #   2. Persisted "sent" log — survives restarts / catches retried events.
    attempt_key = str(attempt.id)
    if attempt_key in _whatsapp_claims:
        logger.info("[WhatsApp] claim already held for call_attempt_id=%s — skipping", attempt.id)
        return
    # Bound memory: once large, clear it — the persisted "sent" log below still
    # catches duplicates for any attempt whose claim was evicted.
    if len(_whatsapp_claims) > 5000:
        _whatsapp_claims.clear()
    _whatsapp_claims.add(attempt_key)

    already_sent = await db.scalar(
        select(WhatsAppLog.id)
        .where(WhatsAppLog.call_attempt_id == attempt.id)
        .where(WhatsAppLog.status == WhatsAppLogStatus.sent)
        .limit(1)
    )
    if already_sent:
        logger.info(
            "[WhatsApp] already sent for call_attempt_id=%s — skipping duplicate", attempt.id
        )
        return

    settings = get_settings()
    if not getattr(settings, "WHATSAPP_ENABLED", False):
        logger.info(
            "[WhatsApp] disabled via WHATSAPP_ENABLED=False — skipping send for call_attempt_id=%s",
            attempt.id,
        )
        await log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=lead.phone,
            template_name=template_name,
            status=WhatsAppLogStatus.skipped,
            error_message="WHATSAPP_ENABLED=False (waiting for Meta business verification)",
        )
        return

    formatted_phone = format_phone_for_exotel_whatsapp(lead.phone)
    if not formatted_phone:
        await log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=lead.phone,
            template_name=template_name,
            status=WhatsAppLogStatus.skipped,
            error_message="invalid or missing phone number",
        )
        return

    try:
        response = await send_whatsapp(
            lead.phone,
            template_name,
            None,
            db,
        )
        await log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=formatted_phone,
            template_name=template_name,
            status=WhatsAppLogStatus.sent,
            response_body=response,
        )
    except Exception as exc:
        await log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=formatted_phone,
            template_name=template_name,
            status=WhatsAppLogStatus.failed,
            error_message=str(exc),
        )
        logger.exception("[WhatsApp] send failed for call_attempt_id=%s", attempt.id)
        if retry_once:
            # Release the claim so the retry attempt can re-acquire it.
            _whatsapp_claims.discard(attempt_key)
            await asyncio.sleep(300)
            await send_whatsapp_for_call(call_attempt_id, db, retry_once=False)


async def send_whatsapp_call_completed(
    lead_name: str,
    phone: str,
    summary: str | None = None,
) -> dict[str, Any] | None:
    return await send_whatsapp(
        phone,
        _template_name(),
        None,
    )


async def send_whatsapp_call_missed(
    lead_name: str,
    phone: str,
) -> dict[str, Any] | None:
    return await send_whatsapp(
        phone,
        _template_name(),
        None,
    )


async def send_whatsapp_custom(
    phone: str,
    text: str,
) -> dict[str, Any] | None:
    """Send a free-text message via Meta Cloud API (requires an active conversation window)."""
    phone_number_id, access_token = _meta_credentials()
    to_number = format_meta_phone(phone)
    if not phone_number_id or not access_token:
        raise RuntimeError("META_WA_PHONE_NUMBER_ID and META_WA_ACCESS_TOKEN must be set in .env")
    if not to_number:
        raise RuntimeError(f"Invalid WhatsApp phone: {phone}")

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(_meta_url(phone_number_id), json=payload, headers=headers)
    response.raise_for_status()
    return _response_body(response)
