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


def format_phone_for_exotel_whatsapp(phone: str | None) -> str | None:
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


def format_wati_phone(phone: str | None) -> str | None:
    formatted = format_phone_for_exotel_whatsapp(phone)
    return formatted.replace("+", "") if formatted else None


def _whatsapp_from_number() -> str | None:
    settings = get_settings()
    raw = (
        settings.EXOTEL_WHATSAPP_FROM_NUMBER
        or settings.EXOTEL_WHATSAPP_NUMBER
        or settings.EXOTEL_WA_PHONE_NUMBER
    )
    return format_phone_for_exotel_whatsapp(raw)


def _whatsapp_credentials() -> tuple[str, str, str, str]:
    settings = get_settings()
    api_key = settings.EXOTEL_WA_API_KEY or settings.EXOTEL_API_KEY or ""
    api_token = settings.EXOTEL_WA_API_TOKEN or settings.EXOTEL_API_TOKEN or ""
    account_sid = settings.EXOTEL_WA_ACCOUNT_SID or settings.EXOTEL_ACCOUNT_SID or ""
    subdomain = settings.EXOTEL_WA_SUBDOMAIN or settings.EXOTEL_SUBDOMAIN or "api.in.exotel.com"
    return api_key, api_token, account_sid, subdomain


def _whatsapp_url() -> str:
    _, _, account_sid, subdomain = _whatsapp_credentials()
    return f"https://{subdomain}/v2/accounts/{account_sid}/messages"


def _template_name() -> str:
    settings = get_settings()
    return settings.EXOTEL_WA_TEMPLATE_SOIL_SYSTEMS or SOIL_SYSTEMS_TEMPLATE


def build_exotel_template_payload(
    *,
    from_number: str,
    to_number: str,
    template_name: str,
    parameters: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    if parameters:
        components.append(
            {
                "type": "body",
                "parameters": parameters,
            }
        )

    message = {
        "from": from_number,
        "to": to_number,
        "content": {
            "recipient_type": "individual",
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": "en",
                    "policy": "deterministic",
                },
                "components": components,
            },
        },
    }
    return {
        "custom_data": str(uuid.uuid4()),
        "whatsapp": {
            "messages": [message],
        },
    }


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
    from_number = _whatsapp_from_number()
    to_number = format_phone_for_exotel_whatsapp(lead_phone)
    if not from_number:
        raise RuntimeError("EXOTEL_WHATSAPP_FROM_NUMBER or EXOTEL_WA_PHONE_NUMBER is not configured")
    if not to_number:
        raise RuntimeError(f"Invalid lead phone for WhatsApp: {lead_phone}")

    payload = build_exotel_template_payload(
        from_number=from_number,
        to_number=to_number,
        template_name=template_name,
        parameters=parameters,
    )
    api_key, api_token, _, _ = _whatsapp_credentials()
    logger.info("[WhatsApp] Exotel payload=%s", json.dumps(payload, indent=2))

    async with httpx.AsyncClient(auth=httpx.BasicAuth(api_key, api_token), timeout=15.0) as client:
        response = await client.post(_whatsapp_url(), json=payload)

    logger.info("[WhatsApp] Exotel response=%s %s", response.status_code, response.text)
    if response.status_code >= 400:
        raise RuntimeError(f"Exotel API failed with status {response.status_code}: {_response_body(response)}")
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
    from_number = _whatsapp_from_number()
    to_number = format_phone_for_exotel_whatsapp(phone)
    if not from_number:
        raise RuntimeError("EXOTEL_WHATSAPP_FROM_NUMBER or EXOTEL_WA_PHONE_NUMBER is not configured")
    if not to_number:
        raise RuntimeError(f"Invalid WhatsApp phone: {phone}")

    payload = {
        "from": from_number,
        "to": to_number,
        "content": {
            "recipient_type": "individual",
            "type": "text",
            "text": {"body": text},
        },
    }
    api_key, api_token, _, _ = _whatsapp_credentials()
    async with httpx.AsyncClient(auth=httpx.BasicAuth(api_key, api_token), timeout=15.0) as client:
        response = await client.post(_whatsapp_url(), json=payload)
    response.raise_for_status()
    return _response_body(response)
