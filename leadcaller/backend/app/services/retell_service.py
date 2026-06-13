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

from app.call_scripts import LANGUAGE_ADAPTATION_INSTRUCTION  # noqa: E402 — re-export for backwards compat


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


async def _resolve_real_inbound_caller(payload: RetellCallCompletedWebhook) -> str | None:
    """Return the real customer phone for an inbound call.

    payload.from_number is our ExoPhone (Exotel uses it as the SIP From when
    bridging), so we go to Exotel's Call resource to find the parent inbound
    call's actual From. Returns None on any error — caller should fall back
    to whatever phone is in the payload.
    """
    from app.services.exotel_service import fetch_real_inbound_caller_phone
    try:
        return await fetch_real_inbound_caller_phone(
            payload.to_number,
            call_started_at=payload.started_at,
        )
    except Exception as exc:
        logger.warning("Failed to resolve real inbound caller phone: %s", exc)
        return None


async def _create_inbound_lead(
    payload: RetellCallCompletedWebhook,
    *,
    real_caller_phone: str | None = None,
    db: AsyncSession,
) -> Lead | None:
    caller_phone = real_caller_phone or payload.from_number or payload.to_number
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

    # Use the Exotel bridge approach: Exotel calls lead (Leg1), bridges to
    # Retell SIP (Leg2). Retell receives it as inbound and the /retell/inbound
    # handler detects the outbound bridge via CrmSyncLog → AI speaks first.
    # This bypasses the broken Retell SIP outbound trunk (missing auth creds).
    try:
        from app.services.exotel_service import connect_exotel_call_with_retell_ai
        result = await connect_exotel_call_with_retell_ai(call_job.lead, db)
        logger.info("Exotel bridge call queued for call_job_id=%s result=%s", call_job.id, result)
    except Exception as exc:
        call_job.status = CallJobStatus.failed
        await db.commit()
        await schedule_retry(call_job.id, "failed", db)
        logger.exception("Exotel bridge call failed for call_job_id=%s: %s", call_job.id, exc)
        return


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
            # Exotel bridges inbound calls to Retell SIP using our ExoPhone as
            # the SIP From — so payload.from_number is OUR number, not the real
            # caller's. Resolve the real caller phone via Exotel before the
            # phone-based lead lookup, otherwise we end up sending the WhatsApp
            # follow-up to our own business number.
            real_caller_phone = await _resolve_real_inbound_caller(payload)
            # NEVER fall back to payload.from_number — for Exotel-bridged inbound
            # calls that IS our own ExoPhone, which would create a self-lead and
            # WhatsApp our own business number (EX_INVALID_REQUEST). If we can't
            # resolve the real caller, skip lead creation entirely; the call is
            # still recorded in Retell, just without a post-call follow-up.
            if not real_caller_phone:
                logger.warning(
                    "Inbound call %s: could not resolve real caller phone — skipping lead creation "
                    "and post-call follow-up (will not message our own ExoPhone)",
                    payload.call_id,
                )
            else:
                lead = (
                    await _find_lead_by_phone(real_caller_phone, db)
                    or await _create_inbound_lead(payload, real_caller_phone=real_caller_phone, db=db)
                )

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
