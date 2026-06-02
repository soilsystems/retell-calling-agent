import asyncio
import logging
import uuid
from dataclasses import dataclass
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

BROADCAST_NAME = "leadcaller_broadcast"
BROCHURE_URL = "https://www.soilsystems.in/_files/ugd/6c151e_1f49d9ce4c1242cdbc5550f67ca0d18d.pdf"

SITE_VISIT_TEMPLATE = "soil_systems_site_visit"
FOLLOWUP_TEMPLATE = "soil_systems_followup"
BROCHURE_TEMPLATE = "soil_systems_brochure"


@dataclass(frozen=True)
class WhatsAppPlan:
    template_name: str | None
    parameters: list[dict[str, str]]
    status: WhatsAppLogStatus
    reason: str | None = None
    attach_brochure: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_wati_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(char for char in phone if char.isdigit())
    if len(digits) == 10 and digits[0] in "6789":
        return f"91{digits}"
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits
    return None


def _clean_name(name: str) -> str:
    return name.replace("(Sample)", "").replace("(sample)", "").replace("Test", "").strip() or name.strip()


def _friendly_datetime(value: Any) -> str:
    if value is None:
        return "the scheduled time"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _site_visit_day(structured: dict[str, Any]) -> str:
    value = structured.get("site_visit_day") or structured.get("site_visit_time") or structured.get("follow_up_time")
    if isinstance(value, datetime):
        return value.strftime("%A")
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%A")
        except ValueError:
            return value
    return "the scheduled day"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def build_whatsapp_plan(lead_name: str, structured: dict[str, Any]) -> WhatsAppPlan:
    settings = get_settings()
    name_param = {"name": "lead_name", "value": lead_name}
    site_visit_agreed = _as_bool(structured.get("site_visit_agreed"))
    follow_up_required = _as_bool(structured.get("follow_up_required"))
    interest_level = str(structured.get("interest_level") or "").strip()

    if site_visit_agreed:
        return WhatsAppPlan(
            template_name=SITE_VISIT_TEMPLATE,
            parameters=[name_param, {"name": "site_visit_day", "value": _site_visit_day(structured)}],
            status=WhatsAppLogStatus.sent,
        )

    if follow_up_required:
        return WhatsAppPlan(
            template_name=FOLLOWUP_TEMPLATE,
            parameters=[name_param, {"name": "follow_up_time", "value": _friendly_datetime(structured.get("follow_up_time"))}],
            status=WhatsAppLogStatus.sent,
        )

    if interest_level in {"Hot", "Warm"}:
        return WhatsAppPlan(
            template_name=BROCHURE_TEMPLATE,
            parameters=[name_param],
            status=WhatsAppLogStatus.sent,
            attach_brochure=True,
        )

    # Always send a post-call follow-up message regardless of interest level or outcome.
    # This is the default fallback — every completed call should notify the customer.
    return WhatsAppPlan(
        template_name=settings.EXOTEL_WA_TEMPLATE_COMPLETED,
        parameters=[name_param],
        status=WhatsAppLogStatus.sent,
    )


async def _load_attempt(call_attempt_id: uuid.UUID, db: AsyncSession) -> CallAttempt | None:
    result = await db.execute(
        select(CallAttempt)
        .options(selectinload(CallAttempt.call_job).selectinload(CallJob.lead))
        .where(CallAttempt.id == call_attempt_id)
    )
    return result.scalar_one_or_none()


async def _log_whatsapp(
    db: AsyncSession,
    *,
    lead_id: uuid.UUID,
    call_attempt_id: uuid.UUID,
    phone: str | None,
    template_name: str | None,
    status: WhatsAppLogStatus,
    wati_response: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> WhatsAppLog:
    log = WhatsAppLog(
        lead_id=lead_id,
        call_attempt_id=call_attempt_id,
        phone=phone,
        template_name=template_name,
        status=status,
        wati_response=wati_response,
        error_message=error_message,
        sent_at=_utcnow(),
    )
    db.add(log)
    await db.commit()
    return log


async def send_whatsapp(
    lead_phone: str,
    template_name: str,
    parameters: list[dict[str, str]],
    db: AsyncSession,
    *,
    attach_brochure: bool = False,
) -> dict[str, Any]:
    settings = get_settings()

    # Convert WATI-like parameter format to flat list of strings for Exotel
    flat_params = [p["value"] for p in parameters]
    if attach_brochure:
        flat_params.append(settings.BOOKING_LINK)

    # Exotel WhatsApp v1 endpoint — credentials embedded in URL (per Exotel docs).
    # The v2/messages endpoint is a multichannel API that requires a "channel" field;
    # the v1/Accounts/Messages endpoint is the dedicated WhatsApp API.
    api_key = settings.EXOTEL_WA_API_KEY or ""
    api_token = settings.EXOTEL_WA_API_TOKEN or ""
    subdomain = settings.EXOTEL_WA_SUBDOMAIN or "api.in.exotel.com"
    account_sid = settings.EXOTEL_WA_ACCOUNT_SID or ""
    url = f"https://{api_key}:{api_token}@{subdomain}/v1/Accounts/{account_sid}/Messages"

    headers = {"Content-Type": "application/json"}

    # Format phone numbers to E.164 format with + prefix
    from_raw = settings.EXOTEL_WA_PHONE_NUMBER or ""
    from_digits = "".join(c for c in from_raw if c.isdigit())
    if len(from_digits) == 10:
        from_number = f"+91{from_digits}"
    elif len(from_digits) == 12 and from_digits.startswith("91"):
        from_number = f"+{from_digits}"
    else:
        from_number = from_raw if from_raw.startswith("+") else f"+{from_raw}"

    to_digits = "".join(c for c in lead_phone if c.isdigit())
    if len(to_digits) == 10:
        to_number = f"+91{to_digits}"
    elif len(to_digits) == 12 and to_digits.startswith("91"):
        to_number = f"+{to_digits}"
    else:
        to_number = lead_phone if lead_phone.startswith("+") else f"+{lead_phone}"

    payload = {
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
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": val} for val in flat_params
                        ],
                    }
                ],
            },
        },
    }

    logger.info(
        "Sending WhatsApp via Exotel v1: to=%s template=%s params=%s",
        to_number, template_name, flat_params,
    )

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=payload)

    response_payload: dict[str, Any]
    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"text": response.text}

    logger.info("Exotel WhatsApp response: status=%s body=%s", response.status_code, response_payload)

    if response.status_code >= 400:
        raise RuntimeError(f"Exotel API failed with status {response.status_code}: {response_payload}")
    return response_payload


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
        logger.warning("WhatsApp skipped because call_attempt_id=%s was not found", call_attempt_id)
        return

    lead: Lead = attempt.call_job.lead
    structured = attempt.structured_data or {}
    clean_name = _clean_name(lead.name)
    plan = build_whatsapp_plan(clean_name, structured)
    formatted_phone = format_wati_phone(lead.phone)

    if plan.status == WhatsAppLogStatus.skipped:
        await _log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=formatted_phone or lead.phone,
            template_name=plan.template_name,
            status=WhatsAppLogStatus.skipped,
            error_message=plan.reason,
        )
        logger.info("WhatsApp skipped for call_attempt_id=%s: %s", attempt.id, plan.reason)
        return

    if not formatted_phone:
        await _log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=lead.phone,
            template_name=plan.template_name,
            status=WhatsAppLogStatus.skipped,
            error_message="invalid or missing phone number",
        )
        return

    try:
        response = await send_whatsapp(
            formatted_phone,
            str(plan.template_name),
            plan.parameters,
            db,
            attach_brochure=plan.attach_brochure,
        )
        log = await _log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=formatted_phone,
            template_name=plan.template_name,
            status=WhatsAppLogStatus.sent,
            wati_response=response,
        )
        from app.services.zoho_service import update_zoho_whatsapp_status

        try:
            await update_zoho_whatsapp_status(log.id, db)
        except Exception:
            logger.exception("Zoho WhatsApp status update failed for whatsapp_log_id=%s", log.id)
    except Exception as exc:
        await _log_whatsapp(
            db,
            lead_id=lead.id,
            call_attempt_id=attempt.id,
            phone=formatted_phone,
            template_name=plan.template_name,
            status=WhatsAppLogStatus.failed,
            error_message=str(exc),
        )
        logger.exception("WhatsApp send failed for call_attempt_id=%s", attempt.id)
        if retry_once:
            await asyncio.sleep(300)
            await send_whatsapp_for_call(call_attempt_id, db, retry_once=False)


async def send_whatsapp_call_completed(
    lead_name: str,
    phone: str,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Send call completed template manually."""
    settings = get_settings()
    clean = _clean_name(lead_name)
    params = [{"name": "lead_name", "value": clean}]
    async with AsyncSessionLocal() as session:
        return await send_whatsapp(phone, settings.EXOTEL_WA_TEMPLATE_COMPLETED, params, session)


async def send_whatsapp_call_missed(
    lead_name: str,
    phone: str,
) -> dict[str, Any] | None:
    """Send missed call template manually."""
    settings = get_settings()
    clean = _clean_name(lead_name)
    params = [{"name": "lead_name", "value": clean}]
    async with AsyncSessionLocal() as session:
        return await send_whatsapp(phone, settings.EXOTEL_WA_TEMPLATE_MISSED, params, session)


async def send_whatsapp_custom(
    phone: str,
    text: str,
) -> dict[str, Any] | None:
    """Send custom free-text WhatsApp message."""
    settings = get_settings()
    api_key = settings.EXOTEL_WA_API_KEY or ""
    api_token = settings.EXOTEL_WA_API_TOKEN or ""
    subdomain = settings.EXOTEL_WA_SUBDOMAIN or "api.in.exotel.com"
    account_sid = settings.EXOTEL_WA_ACCOUNT_SID or ""
    url = f"https://{api_key}:{api_token}@{subdomain}/v1/Accounts/{account_sid}/Messages"
    headers = {"Content-Type": "application/json"}
    # Format phone numbers to E.164 format with + prefix
    from_raw = settings.EXOTEL_WA_PHONE_NUMBER or ""
    from_digits = "".join(c for c in from_raw if c.isdigit())
    if len(from_digits) == 10:
        from_number = f"+91{from_digits}"
    elif len(from_digits) == 12 and from_digits.startswith("91"):
        from_number = f"+{from_digits}"
    else:
        from_number = from_raw if from_raw.startswith("+") else f"+{from_raw}"

    to_digits = "".join(c for c in phone if c.isdigit())
    if len(to_digits) == 10:
        to_number = f"+91{to_digits}"
    elif len(to_digits) == 12 and to_digits.startswith("91"):
        to_number = f"+{to_digits}"
    else:
        to_number = phone if phone.startswith("+") else f"+{phone}"

    payload = {
        "from": from_number,
        "to": to_number,
        "content": {
            "recipient_type": "individual",
            "type": "text",
            "text": {
                "body": text
            }
        }
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, headers=headers, json=payload)
    return response.json()

