import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_db
from app.models import (
    CallAttempt,
    CallJob,
    CallJobStatus,
    CrmSyncLog,
    Followup,
    Lead,
    WebhookEvent,
)
from app.services.exotel_service import connect_exotel_call
from app.services.retell_service import trigger_retell_call
from app.services.zoho_service import sync_recent_zoho_leads

router = APIRouter(prefix="/admin", tags=["admin"])


class CallLeadRequest(BaseModel):
    mode: Literal["ai", "human", "exotel", "exotel_human", "exotel_app"]
    agent_phone: str | None = None


class ZohoSyncResponse(BaseModel):
    fetched: int
    synced: int
    skipped: int


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _status_value(value: Any) -> str:
    return getattr(value, "value", str(value))


async def _queue_retell_ai_call(lead: Lead, db: AsyncSession, background_tasks: BackgroundTasks, mode: str) -> dict[str, Any]:
    from datetime import datetime, timezone

    call_job = CallJob(
        lead_id=lead.id,
        status=CallJobStatus.pending,
        scheduled_at=datetime.now(timezone.utc),
        retry_count=0,
        max_retries=3,
    )
    db.add(call_job)
    await db.commit()
    await db.refresh(call_job)
    background_tasks.add_task(trigger_retell_call, call_job.id)
    return {
        "mode": mode,
        "status": "queued",
        "call_job_id": str(call_job.id),
        "lead_name": lead.name,
        "phone": lead.phone,
    }


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    total_leads = await db.scalar(select(func.count(Lead.id)))
    pending_jobs = await db.scalar(select(func.count(CallJob.id)).where(CallJob.status == CallJobStatus.pending))
    in_progress_jobs = await db.scalar(
        select(func.count(CallJob.id)).where(CallJob.status == CallJobStatus.in_progress)
    )
    completed_jobs = await db.scalar(select(func.count(CallJob.id)).where(CallJob.status == CallJobStatus.completed))
    failed_jobs = await db.scalar(select(func.count(CallJob.id)).where(CallJob.status == CallJobStatus.failed))
    hot_leads = await db.scalar(
        select(func.count(CallAttempt.id)).where(CallAttempt.structured_data["interest_level"].astext == "Hot")
    )
    answered = await db.scalar(
        select(func.count(CallAttempt.id)).where(CallAttempt.status.in_(["answered", "completed"]))
    )
    calls_made = await db.scalar(select(func.count(CallAttempt.id)))
    webhook_backlog = await db.scalar(select(func.count(WebhookEvent.id)).where(WebhookEvent.processed.is_(False)))
    crm_failures = await db.scalar(select(func.count(CrmSyncLog.id)).where(CrmSyncLog.success.is_(False)))

    total_leads = int(total_leads or 0)
    hot_leads = int(hot_leads or 0)
    conversion_rate = round((hot_leads / total_leads) * 100, 2) if total_leads else 0

    return {
        "total_leads": total_leads,
        "pending_jobs": int(pending_jobs or 0),
        "in_progress_jobs": int(in_progress_jobs or 0),
        "completed_jobs": int(completed_jobs or 0),
        "failed_jobs": int(failed_jobs or 0),
        "calls_made": int(calls_made or 0),
        "answered": int(answered or 0),
        "hot_leads": hot_leads,
        "conversion_rate": conversion_rate,
        "webhook_backlog": int(webhook_backlog or 0),
        "crm_failures": int(crm_failures or 0),
    }


@router.post("/zoho/sync", response_model=ZohoSyncResponse)
async def sync_zoho(limit: int = 100, db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    return await sync_recent_zoho_leads(db, limit=limit)


@router.get("/leads")
async def leads(limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    result = await db.execute(
        select(Lead)
        .options(selectinload(Lead.call_jobs).selectinload(CallJob.attempts))
        .order_by(desc(Lead.zoho_lead_id), desc(Lead.updated_at), desc(Lead.created_at))
        .limit(limit)
    )

    rows = []
    for lead in result.scalars().unique():
        latest_job = max(lead.call_jobs, key=lambda job: job.created_at, default=None)
        latest_attempt = None
        if latest_job:
            latest_attempt = max(latest_job.attempts, key=lambda attempt: attempt.attempt_number, default=None)
        rows.append(
            {
                "id": str(lead.id),
                "zoho_lead_id": lead.zoho_lead_id,
                "name": lead.name,
                "phone": lead.phone,
                "email": lead.email,
                "city": lead.city,
                "language_preference": _status_value(lead.language_preference),
                "source": lead.source,
                "campaign": lead.campaign,
                "created_at": _iso(lead.created_at),
                "latest_call_job_status": _status_value(latest_job.status) if latest_job else None,
                "latest_call_job_id": str(latest_job.id) if latest_job else None,
                "latest_attempt_status": _status_value(latest_attempt.status) if latest_attempt else None,
                "latest_interest_level": (latest_attempt.structured_data or {}).get("interest_level")
                if latest_attempt
                else None,
                "latest_summary": latest_attempt.summary if latest_attempt else None,
                "latest_callback_required": (latest_attempt.structured_data or {}).get("callback_required")
                if latest_attempt
                else None,
                "latest_callback_time": (latest_attempt.structured_data or {}).get("callback_time")
                if latest_attempt
                else None,
                "latest_follow_up_required": (latest_attempt.structured_data or {}).get("follow_up_required")
                if latest_attempt
                else None,
                "latest_follow_up_time": (latest_attempt.structured_data or {}).get("follow_up_time")
                if latest_attempt
                else None,
            }
        )
    return rows


@router.get("/call-jobs")
async def call_jobs(limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    result = await db.execute(
        select(CallJob).options(selectinload(CallJob.lead)).order_by(desc(CallJob.created_at)).limit(limit)
    )
    return [
        {
            "id": str(job.id),
            "lead_id": str(job.lead_id),
            "lead_name": job.lead.name if job.lead else None,
            "phone": job.lead.phone if job.lead else None,
            "status": _status_value(job.status),
            "trigger_reason": job.trigger_reason,
            "scheduled_at": _iso(job.scheduled_at),
            "started_at": _iso(job.started_at),
            "completed_at": _iso(job.completed_at),
            "retry_count": job.retry_count,
            "max_retries": job.max_retries,
            "created_at": _iso(job.created_at),
        }
        for job in result.scalars()
    ]


@router.get("/call-attempts")
async def call_attempts(limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    result = await db.execute(
        select(CallAttempt)
        .options(selectinload(CallAttempt.call_job).selectinload(CallJob.lead))
        .order_by(desc(CallAttempt.started_at), desc(CallAttempt.attempt_number))
        .limit(limit)
    )
    rows = []
    for attempt in result.scalars():
        lead = attempt.call_job.lead if attempt.call_job else None
        structured_data = attempt.structured_data or {}
        rows.append(
            {
                "id": str(attempt.id),
                "call_job_id": str(attempt.call_job_id),
                "lead_id": str(lead.id) if lead else None,
                "lead_name": lead.name if lead else None,
                "phone": lead.phone if lead else None,
                "retell_call_id": attempt.retell_call_id,
                "attempt_number": attempt.attempt_number,
                "status": _status_value(attempt.status),
                "direction": _status_value(attempt.direction),
                "recording_url": attempt.recording_url,
                "summary": attempt.summary,
                "transcript": attempt.transcript,
                "structured_data": structured_data,
                "interest_level": structured_data.get("interest_level"),
                "follow_up_required": structured_data.get("follow_up_required"),
                "follow_up_time": structured_data.get("follow_up_time"),
                "callback_required": structured_data.get("callback_required"),
                "callback_time": structured_data.get("callback_time"),
                "call_outcome": structured_data.get("call_outcome"),
                "caller_requirement": structured_data.get("caller_requirement")
                or structured_data.get("caller_details")
                or structured_data.get("requirement")
                or structured_data.get("enquiry_details"),
                "started_at": _iso(attempt.started_at),
                "ended_at": _iso(attempt.ended_at),
                "duration_seconds": attempt.duration_seconds,
            }
        )
    return rows


@router.get("/webhook-events")
async def webhook_events(limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    result = await db.execute(select(WebhookEvent).order_by(desc(WebhookEvent.received_at)).limit(limit))
    return [
        {
            "id": str(event.id),
            "source": _status_value(event.source),
            "event_type": event.event_type,
            "processed": event.processed,
            "idempotency_key": event.idempotency_key,
            "received_at": _iso(event.received_at),
        }
        for event in result.scalars()
    ]


@router.get("/followups")
async def followups(limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    result = await db.execute(
        select(Followup).options(selectinload(Followup.lead)).order_by(desc(Followup.scheduled_at)).limit(limit)
    )
    return [
        {
            "id": str(followup.id),
            "lead_id": str(followup.lead_id),
            "lead_name": followup.lead.name if followup.lead else None,
            "scheduled_at": _iso(followup.scheduled_at),
            "zoho_task_id": followup.zoho_task_id,
            "status": _status_value(followup.status),
        }
        for followup in result.scalars()
    ]


@router.get("/crm-sync-logs")
async def crm_sync_logs(limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    result = await db.execute(
        select(CrmSyncLog).options(selectinload(CrmSyncLog.lead)).order_by(desc(CrmSyncLog.synced_at)).limit(limit)
    )
    return [
        {
            "id": str(log.id),
            "lead_id": str(log.lead_id) if log.lead_id else None,
            "lead_name": log.lead.name if log.lead else None,
            "operation": log.operation,
            "success": log.success,
            "error_message": log.error_message,
            "synced_at": _iso(log.synced_at),
        }
        for log in result.scalars()
    ]


@router.post("/call-jobs/{call_job_id}/trigger")
async def trigger_call_job(
    call_job_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    call_job = await db.get(CallJob, call_job_id)
    if not call_job:
        raise HTTPException(status_code=404, detail="call job not found")
    if call_job.status != CallJobStatus.pending:
        raise HTTPException(status_code=409, detail=f"call job is {call_job.status.value}, not pending")

    background_tasks.add_task(trigger_retell_call, call_job.id)
    return {"status": "queued", "call_job_id": str(call_job.id)}


@router.post("/leads/{lead_id}/call")
async def call_lead(
    lead_id: uuid.UUID,
    body: CallLeadRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Initiate a call for a lead.

    mode=ai        : Retell AI agent places the phone call automatically.
    mode=human     : Bridges the phone call to the human agent's physical phone via Exotel.
    mode=exotel    : Exotel bridges the phone call through the configured ExoML app.
    mode=exotel_human : Exotel bridges the phone call through the configured ExoML app.
    mode=exotel_app: Exotel bridges the phone call through the configured ExoML app.
    """
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="lead not found")

    if body.mode == "ai":
        return await _queue_retell_ai_call(lead, db, background_tasks, body.mode)

    if body.mode in {"exotel", "exotel_app"}:
        return await connect_exotel_call(lead, db)

    # mode == "human" or "exotel_human": Bridge the call to the human agent's phone via Exotel
    if not body.agent_phone or body.agent_phone.strip() == "":
        raise HTTPException(status_code=400, detail="agent_phone is required for human call bridging")

    from app.services.exotel_service import connect_exotel_human_call
    return await connect_exotel_human_call(lead, body.agent_phone.strip(), db)
