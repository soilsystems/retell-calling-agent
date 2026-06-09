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


async def send_whatsapp(
    lead_phone: str,
    template_name: str,
    parameters: list[dict[str, str]] | None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    phone_number_id, access_token = _meta_credentials()
    if not phone_number_id or not access_token:
        raise RuntimeError(
            "META_WA_PHONE_NUMBER_ID and META_WA_ACCESS_TOKEN must be set in .env"
        )

    to_number = format_meta_phone(lead_phone)
    if not to_number:
        raise RuntimeError(f"Invalid lead phone for WhatsApp: {lead_phone}")

    components: list[dict[str, Any]] = []
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

    logger.info("[WhatsApp] Meta payload=%s", json.dumps(payload, indent=2))

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            _meta_url(phone_number_id),
            json=payload,
            headers=headers,
        )

    logger.info("[WhatsApp] Meta response=%s %s", response.status_code, response.text)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Meta Cloud API failed with status {response.status_code}: {_response_body(response)}"
        )
    return _response_body(response)


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
