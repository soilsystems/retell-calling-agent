import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import CallAttempt, CallAttemptStatus, CallJob, CallJobStatus, WebhookEvent, WebhookSource
from app.schemas.retell_schema import RetellCallCompletedWebhook, RetellStructuredData

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    call_job.status = CallJobStatus.in_progress
    call_job.started_at = _utcnow()
    await db.commit()

    body = {
        "from_number": settings.RETELL_FROM_NUMBER,
        "to_number": call_job.lead.phone,
        "agent_id": settings.RETELL_AGENT_ID,
        "retell_llm_dynamic_variables": {
            "lead_name": call_job.lead.name,
            "language": call_job.lead.language_preference.value,
            "city": call_job.lead.city or "",
            "campaign": call_job.lead.campaign or "",
            "zoho_lead_id": call_job.lead.zoho_lead_id,
        },
        "webhook_url": f"{settings.BASE_URL.rstrip('/')}/webhooks/retell/call-completed",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.retellai.com/v2/create-phone-call",
                headers={"Authorization": f"Bearer {settings.RETELL_API_KEY}"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
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
        started_at=_utcnow(),
    )
    db.add(attempt)
    await db.commit()


async def schedule_retry(
    call_job_id: uuid.UUID,
    failure_reason: str,
    db: AsyncSession | None = None,
) -> None:
    if db is None:
        async with AsyncSessionLocal() as session:
            await schedule_retry(call_job_id, failure_reason, session)
        return

    call_job = await db.get(CallJob, call_job_id)
    if not call_job:
        return

    if call_job.retry_count >= call_job.max_retries:
        call_job.status = CallJobStatus.cancelled
        await db.commit()
        logger.warning("Max retries reached for call_job_id=%s", call_job_id)
        return

    delays = {
        "no_answer": timedelta(hours=2),
        "busy": timedelta(minutes=30),
        "failed": timedelta(minutes=15),
    }
    call_job.retry_count += 1
    call_job.status = CallJobStatus.pending
    call_job.scheduled_at = _utcnow() + delays.get(failure_reason, timedelta(minutes=15))
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
    result = await db.execute(
        select(CallAttempt)
        .options(selectinload(CallAttempt.call_job))
        .where(CallAttempt.retell_call_id == payload.call_id)
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        logger.warning("No call_attempt found for retell_call_id=%s", payload.call_id)
        webhook_event.processed = True
        await db.commit()
        return None

    if attempt.ended_at and attempt.status in {
        CallAttemptStatus.completed,
        CallAttemptStatus.no_answer,
        CallAttemptStatus.busy,
        CallAttemptStatus.failed,
    }:
        webhook_event.processed = True
        await db.commit()
        return attempt

    mapped_status = _map_retell_status(payload.call_status)
    attempt.status = mapped_status
    attempt.transcript = payload.transcript
    attempt.summary = payload.summary
    attempt.recording_url = payload.recording_url
    attempt.structured_data = _structured_dict(payload.structured_data)
    attempt.started_at = payload.started_at or attempt.started_at
    attempt.ended_at = payload.ended_at or _utcnow()
    attempt.duration_seconds = payload.duration_seconds

    attempt.call_job.status = CallJobStatus.completed
    attempt.call_job.completed_at = _utcnow()
    webhook_event.processed = True
    await db.commit()
    await db.refresh(attempt)
    return attempt


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
