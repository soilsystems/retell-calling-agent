import asyncio
import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import (
    CallAttempt,
    CallAttemptStatus,
    CallDirection,
    CallJob,
    CallJobStatus,
    LanguagePreference,
    Lead,
    WebhookEvent,
)
from app.schemas.retell_schema import RetellCallCompletedWebhook, RetellStructuredData
from app.utils.business_hours import get_next_business_day_at_10am, is_business_hours, next_business_slot

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _should_ignore_business_hours(call_job: CallJob) -> bool:
    return getattr(call_job, "trigger_reason", None) in {"new_lead", "new_lead_simulated"}


def _phone_suffix(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(char for char in value if char.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


async def _find_lead_by_phone(phone: str | None, db: AsyncSession) -> Lead | None:
    suffix = _phone_suffix(phone)
    if not suffix:
        return None
    result = await db.execute(select(Lead).where(Lead.phone.like(f"%{suffix}")).limit(1))
    return result.scalars().first()


async def _create_inbound_lead(payload: RetellCallCompletedWebhook, db: AsyncSession) -> Lead | None:
    caller_phone = payload.from_number or payload.to_number
    if not caller_phone:
        return None

    from app.services.zoho_service import create_zoho_lead_for_inbound

    zoho_lead_id = await create_zoho_lead_for_inbound(caller_phone, db)
    lead = Lead(
        zoho_lead_id=zoho_lead_id,
        name="Unknown",
        phone=caller_phone,
        language_preference=LanguagePreference.english,
        source="Inbound Call",
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return lead


async def _schedule_callback_if_requested(
    attempt: CallAttempt,
    structured: dict[str, Any],
    db: AsyncSession,
) -> None:
    if structured.get("callback_required") is not True:
        return

    callback_time = structured.get("callback_time")
    if not callback_time:
        return

    scheduled_at = _parse_callback_time(callback_time, getattr(attempt, "ended_at", None) or _utcnow())
    if scheduled_at is None:
        logger.warning("Invalid callback_time for call_attempt_id=%s: %s", attempt.id, callback_time)
        return

    existing_callback = await db.execute(
        select(CallJob)
        .where(CallJob.lead_id == attempt.call_job.lead_id)
        .where(CallJob.trigger_reason == "callback_requested")
        .where(CallJob.status.in_([CallJobStatus.pending, CallJobStatus.in_progress]))
        .limit(1)
    )
    if existing_callback.scalar_one_or_none():
        logger.info("Callback job already exists for lead_id=%s", attempt.call_job.lead_id)
        return

    call_job = CallJob(
        lead_id=attempt.call_job.lead_id,
        status=CallJobStatus.pending,
        scheduled_at=_ensure_aware_utc(scheduled_at),
        retry_count=0,
        max_retries=3,
        trigger_reason="callback_requested",
    )
    db.add(call_job)
    await db.commit()


def _parse_callback_time(value: Any, reference_time: datetime) -> datetime | None:
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    normalized = text.lower()
    reference = _ensure_aware_utc(reference_time)
    match = re.search(r"(?:after|in)?\s*(?:about\s*)?(\d+|one|two|three|four|five|ten|fifteen|thirty)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)", normalized)
    if not match:
        return None

    numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "ten": 10,
        "fifteen": 15,
        "thirty": 30,
    }
    amount = numbers.get(match.group(1), int(match.group(1)) if match.group(1).isdigit() else None)
    if amount is None:
        return None

    unit = match.group(2)
    if unit.startswith(("hour", "hr")):
        return reference + timedelta(hours=amount)
    return reference + timedelta(minutes=amount)


def _has_completion_enrichment(payload: RetellCallCompletedWebhook, structured: dict[str, Any]) -> bool:
    return bool(
        structured
        or payload.transcript
        or payload.summary
        or payload.recording_url
        or payload.duration_seconds
    )


def _first_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


async def _update_inbound_lead_details(
    lead: Lead | None,
    structured: dict[str, Any],
    db: AsyncSession,
) -> None:
    if not lead:
        return

    if lead.source != "Inbound Call" and lead.name.lower() != "unknown":
        return

    caller_name = _first_text(
        structured,
        "caller_name",
        "customer_name",
        "lead_name",
        "name",
        "full_name",
    )
    caller_city = _first_text(structured, "caller_city", "city", "location")
    caller_email = _first_text(structured, "caller_email", "email")
    caller_requirement = _first_text(
        structured,
        "caller_requirement",
        "caller_details",
        "requirement",
        "enquiry_details",
        "notes",
    )

    changed = False
    if caller_name and lead.name.lower() == "unknown":
        lead.name = caller_name[:100]
        changed = True
    if caller_city and not lead.city:
        lead.city = caller_city[:120]
        changed = True
    if caller_email and not lead.email:
        lead.email = caller_email[:320]
        changed = True
    if caller_requirement and not lead.campaign:
        lead.campaign = caller_requirement[:120]
        changed = True

    if changed:
        await db.commit()
        logger.info("Updated inbound lead details lead_id=%s name=%s city=%s", lead.id, lead.name, lead.city)


async def trigger_retell_call(call_job_id: uuid.UUID, db: AsyncSession | None = None) -> None:
    if db is None:
        async with AsyncSessionLocal() as session:
            await trigger_retell_call(call_job_id, session)
        return

    settings = get_settings()
    result = await db.execute(
        select(CallJob).options(selectinload(CallJob.lead), selectinload(CallJob.attempts)).where(CallJob.id == call_job_id)
    )
    call_job = result.scalar_one_or_none()
    if not call_job or call_job.status != CallJobStatus.pending:
        logger.info("Retell trigger aborted for call_job_id=%s", call_job_id)
        return

    now = _utcnow()
    scheduled_at = _ensure_aware_utc(call_job.scheduled_at)
    if scheduled_at > now:
        logger.info("Retell trigger deferred for call_job_id=%s scheduled_at=%s", call_job.id, scheduled_at)
        return

    if not _should_ignore_business_hours(call_job) and not is_business_hours(now):
        call_job.scheduled_at = next_business_slot(now)
        await db.commit()
        logger.info("Retell trigger moved to next business slot for call_job_id=%s", call_job.id)
        return

    call_job.status = CallJobStatus.in_progress
    call_job.started_at = now
    await db.commit()

    clean_name = call_job.lead.name.replace("(Sample)", "").replace("(sample)", "").replace("Test", "").strip()
    outbound_begin_message = (
        f"Hello, am I speaking with {clean_name}? "
        "This is Vikas calling from Soil Systems about your land investment enquiry."
    )
    outbound_script = (
        "Outbound callback/sales call. Start by confirming the lead is available, "
        "then remind them they had enquired about Soil Systems land investment. "
        "Do not thank them for calling. Ask whether they want details, a brochure, "
        "or a site visit."
    )
    variables = {
        "lead_name": clean_name,
        "customer_name": clean_name,
        "name": clean_name,
        "agent_name": "Vikas",
        "language": call_job.lead.language_preference.value,
        "city": call_job.lead.city or "",
        "campaign": call_job.lead.campaign or "",
        "zoho_lead_id": call_job.lead.zoho_lead_id,
        "call_direction": "outbound",
        "inbound_call": "false",
        "outbound_bridge_call": "true",
        "call_context": "outbound",
        "call_script": outbound_script,
        "conversation_script": outbound_script,
        "opening_instruction": "You placed this outbound callback call to the lead.",
    }
    body = {
        "from_number": settings.RETELL_FROM_NUMBER,
        "to_number": call_job.lead.phone,
        "override_agent_id": settings.RETELL_AGENT_ID,
        "retell_llm_dynamic_variables": variables,
        "agent_override": {
            "retell_llm": {
                "begin_message": outbound_begin_message,
                "general_prompt": outbound_script,
            },
            "conversation_flow": {
                "begin_message": outbound_begin_message,
                "global_prompt": outbound_script,
            },
        },
        "metadata": {
            "lead_id": str(call_job.lead.id),
            "call_job_id": str(call_job.id),
            "trigger_reason": call_job.trigger_reason or "new_lead",
        },
        "webhook_url": f"{settings.BASE_URL.rstrip('/')}/webhooks/retell/call-completed",
    }
    if settings.RETELL_AGENT_VERSION is not None:
        body["override_agent_version"] = settings.RETELL_AGENT_VERSION

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.retellai.com/v2/create-phone-call",
                headers={"Authorization": f"Bearer {settings.RETELL_API_KEY}"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404 and await _fallback_to_exotel_call(call_job, db):
            return
        call_job.status = CallJobStatus.failed
        await db.commit()
        await schedule_retry(call_job.id, "failed", db)
        logger.exception("Retell call creation failed for call_job_id=%s: %s", call_job.id, exc)
        return
    except httpx.HTTPError as exc:
        call_job.status = CallJobStatus.failed
        await db.commit()
        await schedule_retry(call_job.id, "failed", db)
        logger.exception("Retell call creation failed for call_job_id=%s: %s", call_job.id, exc)
        return

    retell_call_id = data.get("call_id") or data.get("retell_call_id")
    if not retell_call_id:
        call_job.status = CallJobStatus.failed
        await db.commit()
        await schedule_retry(call_job.id, "failed", db)
        logger.error("Retell response missing call id for call_job_id=%s", call_job.id)
        return

    attempt_count = await db.scalar(
        select(func.count(CallAttempt.id)).where(CallAttempt.call_job_id == call_job.id)
    )
    attempt = CallAttempt(
        call_job_id=call_job.id,
        retell_call_id=retell_call_id,
        attempt_number=int(attempt_count or 0) + 1,
        status=CallAttemptStatus.initiated,
        direction=CallDirection.outbound,
        started_at=_utcnow(),
    )
    db.add(attempt)
    await db.commit()


async def _fallback_to_exotel_call(call_job: CallJob, db: AsyncSession) -> bool:
    try:
        from app.services.exotel_service import connect_exotel_call

        logger.warning(
            "Retell direct outbound returned 404 for call_job_id=%s; falling back to Exotel bridge",
            call_job.id,
        )
        await connect_exotel_call(call_job.lead, db)
        return True
    except Exception as exc:
        logger.exception("Exotel fallback failed for call_job_id=%s: %s", call_job.id, exc)
        return False


async def schedule_retry(
    call_job_id: uuid.UUID,
    failure_reason: str,
    db: AsyncSession | None = None,
    scheduled_at: datetime | None = None,
) -> None:
    if db is None:
        async with AsyncSessionLocal() as session:
            await schedule_retry(call_job_id, failure_reason, session, scheduled_at=scheduled_at)
        return

    call_job = await db.get(CallJob, call_job_id)
    if not call_job:
        return

    retry_count = call_job.retry_count or 0
    max_retries = call_job.max_retries or 3
    if retry_count >= max_retries:
        call_job.status = CallJobStatus.cancelled
        await db.commit()
        logger.warning("Max retries reached for call_job_id=%s", call_job_id)
        return

    if scheduled_at is None:
        if failure_reason == "no_answer":
            scheduled_at = get_next_business_day_at_10am()
        elif failure_reason == "busy":
            scheduled_at = _utcnow() + timedelta(minutes=30)
        elif failure_reason == "failed":
            scheduled_at = _utcnow() + timedelta(minutes=15)
        else:
            scheduled_at = _utcnow() + timedelta(minutes=15)

    trigger_reasons = {
        "no_answer": "no_answer_retry",
        "busy": "busy_retry",
        "failed": "failed_retry",
    }
    call_job.retry_count = retry_count + 1
    call_job.status = CallJobStatus.pending
    call_job.scheduled_at = _ensure_aware_utc(scheduled_at)
    call_job.trigger_reason = trigger_reasons.get(failure_reason, f"{failure_reason}_retry")
    await db.commit()


def _structured_dict(value: RetellStructuredData | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, RetellStructuredData):
        return value.model_dump(mode="json")
    return value


async def process_retell_completion(
    payload: RetellCallCompletedWebhook,
    webhook_event: WebhookEvent,
    db: AsyncSession,
) -> CallAttempt | None:
    direction = CallDirection.inbound if payload.direction.lower() == "inbound" else CallDirection.outbound
    result = await db.execute(
        select(CallAttempt)
        .options(selectinload(CallAttempt.call_job).selectinload(CallJob.lead))
        .where(CallAttempt.retell_call_id == payload.call_id)
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        lead_id_str = (payload.metadata or {}).get("lead_id") if payload.metadata else None
        lead: Lead | None = None
        if lead_id_str:
            try:
                lead_id = uuid.UUID(lead_id_str)
                lead = await db.get(Lead, lead_id)
            except (ValueError, AttributeError) as exc:
                logger.warning("Failed to parse metadata lead_id for call: %s", exc)

        if not lead and direction == CallDirection.inbound:
            lead = await _find_lead_by_phone(payload.from_number, db) or await _find_lead_by_phone(payload.to_number, db)
            if not lead:
                lead = await _create_inbound_lead(payload, db)

        if lead:
            job_result = await db.execute(
                select(CallJob)
                .where(CallJob.lead_id == lead.id)
                .order_by(CallJob.created_at.desc())
                .limit(1)
            )
            call_job = job_result.scalar_one_or_none()
            if not call_job:
                call_job = CallJob(
                    lead_id=lead.id,
                    status=CallJobStatus.in_progress,
                    scheduled_at=payload.started_at or _utcnow(),
                    retry_count=0,
                    max_retries=3,
                    trigger_reason="inbound_call" if direction == CallDirection.inbound else "new_lead",
                )
                db.add(call_job)
                await db.commit()
                await db.refresh(call_job)

            attempt_count = await db.scalar(select(func.count(CallAttempt.id)).where(CallAttempt.call_job_id == call_job.id))
            attempt = CallAttempt(
                call_job_id=call_job.id,
                retell_call_id=payload.call_id,
                attempt_number=int(attempt_count or 0) + 1,
                status=CallAttemptStatus.initiated,
                direction=direction,
                started_at=payload.started_at or _utcnow(),
            )
            db.add(attempt)
            await db.commit()
            result = await db.execute(
                select(CallAttempt)
                .options(selectinload(CallAttempt.call_job).selectinload(CallJob.lead))
                .where(CallAttempt.id == attempt.id)
            )
            attempt = result.scalar_one()

    if not attempt:
        logger.warning("No call_attempt found and could not create one dynamically for retell_call_id=%s", payload.call_id)
        webhook_event.processed = True
        await db.commit()
        return None

    structured = _structured_dict(payload.structured_data)
    if attempt.ended_at and attempt.status in {
        CallAttemptStatus.completed,
        CallAttemptStatus.no_answer,
        CallAttemptStatus.busy,
        CallAttemptStatus.failed,
    } and not _has_completion_enrichment(payload, structured):
        webhook_event.processed = True
        await db.commit()
        return attempt

    mapped_status = _map_retell_status(payload.call_status)
    attempt.status = mapped_status
    attempt.direction = direction
    attempt.transcript = payload.transcript
    attempt.summary = payload.summary
    attempt.recording_url = payload.recording_url
    attempt.structured_data = structured
    attempt.started_at = payload.started_at or attempt.started_at
    attempt.ended_at = payload.ended_at or _utcnow()
    attempt.duration_seconds = payload.duration_seconds

    attempt.call_job.status = CallJobStatus.completed
    attempt.call_job.completed_at = _utcnow()
    webhook_event.processed = True
    await db.commit()
    await db.refresh(attempt)
    await _update_inbound_lead_details(attempt.call_job.lead, attempt.structured_data or {}, db)
    await _schedule_callback_if_requested(attempt, attempt.structured_data or {}, db)
    return attempt


async def run_scheduled_calls() -> None:
    async with AsyncSessionLocal() as db:
        try:
            now = _utcnow()
            result = await db.execute(
                select(CallJob)
                .where(CallJob.status == CallJobStatus.pending)
                .where(CallJob.scheduled_at <= now)
                .order_by(CallJob.scheduled_at)
                .limit(10)
            )
            jobs = result.scalars().all()

            for job in jobs:
                logger.info("[Scheduler] Triggering call for job %s", job.id)
                await trigger_retell_call(job.id)
        except Exception as exc:
            logger.error("[Scheduler] run_scheduled_calls failed: %s", exc, exc_info=True)


def _map_retell_status(status: str) -> CallAttemptStatus:
    normalized = status.lower().replace("-", "_")
    if normalized in {"not_connected", "no_answer"}:
        return CallAttemptStatus.no_answer
    if normalized == "busy":
        return CallAttemptStatus.busy
    if normalized in {"error", "failed"}:
        return CallAttemptStatus.failed
    if normalized in {"completed", "ended"}:
        return CallAttemptStatus.completed
    if normalized == "ringing":
        return CallAttemptStatus.ringing
    if normalized == "answered":
        return CallAttemptStatus.answered
    return CallAttemptStatus.completed


def retell_event_key(call_id: str, payload_bytes: bytes) -> str:
    digest = hashlib.sha256(payload_bytes).hexdigest()
    return hashlib.sha256(f"retell:{call_id}:{digest}".encode("utf-8")).hexdigest()
