import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import CrmSyncLog, Lead, WebhookEvent, WebhookSource
from app.config import get_settings
from app.schemas.lead_schema import ZohoLeadWebhook
from app.schemas.retell_schema import RetellCallCompletedWebhook
from app.services.lead_service import schedule_call_for_lead
from app.services.retell_service import process_retell_completion, retell_event_key, schedule_retry, trigger_retell_call
from app.services.zoho_service import create_followup_task, sync_to_zoho
from app.utils.security import generate_idempotency_key, verify_retell_signature, verify_zoho_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/zoho/new-lead")
async def zoho_new_lead(
    request: Request,
    background_tasks: BackgroundTasks,
    x_zoho_webhook_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    body = await request.body()
    if not verify_zoho_signature(body, x_zoho_webhook_token):
        logger.warning("Invalid Zoho webhook signature from client=%s", request.client.host if request.client else None)
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})

    try:
        payload = ZohoLeadWebhook.model_validate_json(body)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": json.loads(exc.json())})

    received_at = payload.received_at or datetime.now(timezone.utc)
    idempotency_key = generate_idempotency_key(payload.zoho_lead_id, received_at)
    existing_event = (
        await db.execute(select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing_event and existing_event.processed:
        return JSONResponse(status_code=200, content={"status": "already handled"})

    webhook_event = existing_event or WebhookEvent(
        source=WebhookSource.zoho,
        event_type="new_lead",
        payload=payload.model_dump(mode="json"),
        processed=False,
        idempotency_key=idempotency_key,
        received_at=received_at,
    )
    if not existing_event:
        db.add(webhook_event)
        await db.commit()
        await db.refresh(webhook_event)

    message, call_job = await schedule_call_for_lead(payload, webhook_event, db, now=received_at)
    if message == "call already scheduled":
        return JSONResponse(status_code=200, content={"status": "call already scheduled"})

    background_tasks.add_task(trigger_retell_call, call_job.id)
    return JSONResponse(
        status_code=200,
        content={
            "status": "scheduled",
            "call_job_id": str(call_job.id),
            "scheduled_at": call_job.scheduled_at.isoformat(),
        },
    )


@router.post("/retell/call-completed")
async def retell_call_completed(
    request: Request,
    background_tasks: BackgroundTasks,
    x_retell_signature: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    body = await request.body()
    if not verify_retell_signature(body, x_retell_signature):
        logger.warning("Invalid Retell webhook signature from client=%s", request.client.host if request.client else None)
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})

    try:
        payload = RetellCallCompletedWebhook.model_validate_json(body)
    except ValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": json.loads(exc.json())})

    idempotency_key = retell_event_key(payload.call_id, body)
    existing_event = (
        await db.execute(select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key))
    ).scalar_one_or_none()
    if existing_event and existing_event.processed:
        return JSONResponse(status_code=200, content={"status": "already handled"})

    webhook_event = existing_event or WebhookEvent(
        source=WebhookSource.retell,
        event_type="call_completed",
        payload=json.loads(body.decode("utf-8")),
        processed=False,
        idempotency_key=idempotency_key,
        received_at=datetime.now(timezone.utc),
    )
    if not existing_event:
        db.add(webhook_event)
        await db.commit()
        await db.refresh(webhook_event)

    attempt = await process_retell_completion(payload, webhook_event, db)
    if attempt:
        background_tasks.add_task(sync_to_zoho, attempt.id)
        structured = attempt.structured_data or {}
        if structured.get("follow_up_required"):
            background_tasks.add_task(create_followup_task, attempt.id)
        if attempt.status.value in {"no_answer", "busy", "failed"}:
            background_tasks.add_task(schedule_retry, attempt.call_job_id, attempt.status.value)

    return JSONResponse(status_code=200, content={"status": "accepted"})


@router.post("/exotel/status")
async def exotel_status(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        payload = {"body": (await request.body()).decode("utf-8", errors="replace")}

    logger.info("Exotel status callback received: %s", payload)
    return JSONResponse(status_code=200, content={"status": "accepted"})


@router.post("/retell/inbound")
async def retell_inbound(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "invalid json"})

    logger.info("Retell inbound webhook received payload: %s", payload)
    call_inbound = payload.get("call_inbound") or {}
    candidate_numbers = [
        payload.get("from_number"),
        payload.get("to_number"),
        call_inbound.get("from_number"),
        call_inbound.get("to_number"),
    ]
    lead = await _find_lead_for_retell_inbound(candidate_numbers, db)

    if not lead:
        logger.info("No lead found for Retell inbound candidate numbers=%s", candidate_numbers)
        return JSONResponse(status_code=200, content={"call_inbound": {}})

    logger.info("Found matching lead for inbound call: name=%s", lead.name)
    clean_name = lead.name.replace("(Sample)", "").replace("(sample)", "").replace("Test", "").strip()

    variables = {
        "lead_name": clean_name,
        "customer_name": clean_name,
        "name": clean_name,
        "phone": lead.phone,
        "language": lead.language_preference.value,
        "city": lead.city or "",
        "campaign": lead.campaign or "",
        "zoho_lead_id": lead.zoho_lead_id,
    }

    return JSONResponse(
        status_code=200,
        content={
            "call_inbound": {
                "override_agent_id": get_settings().RETELL_AGENT_ID,
                "dynamic_variables": variables,
                "agent_override": {
                    "retell_llm": {
                        "begin_message": (
                            f"Hello, am I speaking with {clean_name}? "
                            "This is Viraj calling from Soil Systems."
                        )
                    },
                    "conversation_flow": {
                        "begin_message": (
                            f"Hello, am I speaking with {clean_name}? "
                            "This is Viraj calling from Soil Systems."
                        )
                    },
                },
                "metadata": {
                    "lead_id": str(lead.id),
                    "zoho_lead_id": lead.zoho_lead_id,
                    "source": "leadcaller_retell_inbound",
                },
            }
        },
    )


async def _find_lead_for_retell_inbound(candidate_numbers: list[str | None], db: AsyncSession) -> Lead | None:
    for raw_number in candidate_numbers:
        if not raw_number:
            continue
        digits = "".join(c for c in raw_number if c.isdigit())
        suffix = digits[-10:] if len(digits) >= 10 else digits
        if not suffix:
            continue
        result = await db.execute(
            select(Lead).where((Lead.phone == raw_number) | (Lead.phone.like(f"%{suffix}"))).limit(1)
        )
        lead = result.scalars().first()
        if lead:
            return lead

    result = await db.execute(
        select(CrmSyncLog)
        .options(selectinload(CrmSyncLog.lead))
        .where(CrmSyncLog.operation == "exotel_connect_call", CrmSyncLog.success.is_(True))
        .order_by(desc(CrmSyncLog.synced_at))
        .limit(1)
    )
    latest_exotel_log = result.scalar_one_or_none()
    return latest_exotel_log.lead if latest_exotel_log else None

