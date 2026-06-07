import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    CallAttempt,
    CallAttemptStatus,
    CallJob,
    CallJobStatus,
    CrmSyncLog,
    LanguagePreference,
    Lead,
    WebhookEvent,
    WebhookSource,
)
from app.config import get_settings
from app.schemas.lead_schema import ZohoLeadWebhook
from app.schemas.retell_schema import RetellCallCompletedWebhook
from app.services.lead_service import schedule_call_for_lead
from app.services.retell_service import (
    LANGUAGE_ADAPTATION_INSTRUCTION,
    process_retell_completion,
    retell_event_key,
    schedule_retry,
    trigger_retell_call,
)
from app.services.whatsapp_service import send_whatsapp_for_call
from app.services.zoho_service import create_followup_task, create_zoho_lead_for_inbound, sync_to_zoho
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
        background_tasks.add_task(send_whatsapp_for_call, attempt.id)
        structured = attempt.structured_data or {}
        if structured.get("follow_up_required"):
            background_tasks.add_task(create_followup_task, attempt.id)
        if attempt.status.value in {"no_answer", "busy", "failed"}:
            background_tasks.add_task(schedule_retry, attempt.call_job_id, attempt.status.value)

    return JSONResponse(status_code=200, content={"status": "accepted"})


@router.post("/exotel/status")
async def exotel_status(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await request.json()
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        payload = {"body": (await request.body()).decode("utf-8", errors="replace")}

    logger.info("Exotel status callback received: %s", payload)
    status = _exotel_status(payload)
    if status not in {"completed", "answered"}:
        return JSONResponse(status_code=200, content={"status": "accepted", "call_status": status})

    lead = await _find_lead_for_exotel_status(payload, db)
    if not lead:
        logger.warning("Exotel status callback could not resolve lead: %s", payload)
        return JSONResponse(status_code=200, content={"status": "accepted", "whatsapp": "lead_not_found"})

    attempt = await _ensure_exotel_call_attempt(lead, payload, db)
    background_tasks.add_task(send_whatsapp_for_call, attempt.id)
    return JSONResponse(
        status_code=200,
        content={"status": "accepted", "whatsapp": "queued", "call_attempt_id": str(attempt.id)},
    )


def _payload_value(payload: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None and value != "":
            return value
    return None


def _exotel_status(payload: dict[str, Any]) -> str:
    raw = _payload_value(payload, "CallStatus", "Status", "call_status", "status") or ""
    return str(raw).strip().lower().replace("-", "_")


def _exotel_custom_field(payload: dict[str, Any]) -> dict[str, Any]:
    raw = _payload_value(payload, "CustomField", "custom_field", "customfield")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"lead_name": raw}
        return data if isinstance(data, dict) else {}
    return {}


def _phone_suffix(value: Any) -> str | None:
    if not value:
        return None
    digits = "".join(char for char in str(value) if char.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


async def _find_lead_for_exotel_status(payload: dict[str, Any], db: AsyncSession) -> Lead | None:
    custom = _exotel_custom_field(payload)
    lead_id = custom.get("lead_id")
    if lead_id:
        try:
            lead = await db.get(Lead, uuid.UUID(str(lead_id)))
            if lead:
                return lead
        except ValueError:
            logger.warning("Invalid lead_id in Exotel CustomField: %s", lead_id)

    candidates = [
        custom.get("lead_phone"),
        _payload_value(payload, "lead_phone", "To", "From", "PhoneNumber", "Called", "Caller"),
    ]
    for candidate in candidates:
        suffix = _phone_suffix(candidate)
        if not suffix:
            continue
        result = await db.execute(select(Lead).where(Lead.phone.like(f"%{suffix}")).limit(1))
        lead = result.scalars().first()
        if lead:
            return lead
    return None


async def _ensure_exotel_call_attempt(
    lead: Lead,
    payload: dict[str, Any],
    db: AsyncSession,
) -> CallAttempt:
    call_sid = _payload_value(payload, "CallSid", "Sid", "CallUUID", "CallUuid", "call_sid") or str(uuid.uuid4())
    retell_call_id = f"exotel:{call_sid}"
    result = await db.execute(
        select(CallAttempt)
        .options(selectinload(CallAttempt.call_job))
        .where(CallAttempt.retell_call_id == retell_call_id)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    job_result = await db.execute(
        select(CallJob).where(CallJob.lead_id == lead.id).order_by(desc(CallJob.created_at)).limit(1)
    )
    call_job = job_result.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if not call_job:
        call_job = CallJob(
            lead_id=lead.id,
            status=CallJobStatus.completed,
            scheduled_at=now,
            started_at=now,
            completed_at=now,
        )
        db.add(call_job)
        await db.flush()
    else:
        call_job.status = CallJobStatus.completed
        call_job.completed_at = call_job.completed_at or now

    attempt_count = await db.scalar(select(func.count(CallAttempt.id)).where(CallAttempt.call_job_id == call_job.id))
    attempt = CallAttempt(
        call_job_id=call_job.id,
        retell_call_id=retell_call_id,
        attempt_number=int(attempt_count or 0) + 1,
        status=CallAttemptStatus.completed,
        structured_data={"source": "exotel", "status_callback": payload},
        started_at=now,
        ended_at=now,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


@router.post("/exotel/bridge")
async def exotel_bridge(
    request: Request,
) -> Response:
    """ExoML endpoint for agent-first human bridge calls.

    Exotel calls the agent first (Leg1) using Connect-to-Flow.
    When the agent picks up, Exotel POSTs to this URL and we return ExoML
    that Dials the lead (Leg2) — bridging both with full bidirectional audio.

    Exotel sends call metadata as form-data; we read lead_phone and caller_id
    from query params that we embedded in the URL when initiating the call.

    Query params (embedded in Url at call initiation):
        lead_phone   - E.164 phone number of the lead to dial.
        caller_id    - ExoPhone number to show on the lead's screen.
    """
    lead_phone = request.query_params.get("lead_phone", "")
    caller_id = request.query_params.get("caller_id", "")

    # Defensive fallback: check form data if not found in query parameters
    if not lead_phone or not caller_id:
        try:
            form = await request.form()
            custom_field = form.get("CustomField", "")
            
            # If CustomField contains JSON, parse it
            if custom_field.startswith("{"):
                import json
                try:
                    custom_data = json.loads(custom_field)
                    lead_phone = lead_phone or custom_data.get("lead_phone", "")
                    caller_id = caller_id or custom_data.get("caller_id", "")
                except json.JSONDecodeError:
                    pass
            
            if not lead_phone:
                lead_phone = form.get("lead_phone", "") or custom_field
            if not caller_id:
                caller_id = form.get("caller_id", "") or form.get("CallerId", "")
        except Exception as e:
            logger.debug("Failed to parse form data in exotel_bridge fallback: %s", e)

    if not lead_phone:
        logger.warning("ExoML bridge called without lead_phone")
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response><Hangup/></Response>"""
        return Response(content=xml, media_type="application/xml")

    logger.info("ExoML bridge: dialing lead=%s via caller_id=%s", lead_phone, caller_id)

    caller_id_attr = f' callerId="{caller_id}"' if caller_id else ""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial{caller_id_attr}>{lead_phone}</Dial>
</Response>"""
    return Response(content=xml, media_type="application/xml")


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
    is_new_inbound_lead = False

    if not lead:
        caller_phone = payload.get("from_number") or call_inbound.get("from_number")
        if not caller_phone:
            logger.info("No caller phone found for Retell inbound payload=%s", payload)
            return JSONResponse(status_code=200, content={"call_inbound": {}})
        lead = await _create_unknown_inbound_lead(caller_phone, db)
        is_new_inbound_lead = True
        logger.info("Created unknown inbound lead_id=%s phone=%s", lead.id, lead.phone)

    logger.info("Found matching lead for inbound call: name=%s", lead.name)
    clean_name = lead.name.replace("(Sample)", "").replace("(sample)", "").replace("Test", "").strip()
    settings = get_settings()
    is_outbound_bridge = False if is_new_inbound_lead else await _is_recent_exotel_outbound_for_lead(lead, db)
    call_direction = "outbound" if is_outbound_bridge else "inbound"

    outbound_bridge_script = (
        "Outbound callback/sales call. Start by confirming the lead is available, "
        "then remind them they had enquired about Soil Systems land investment. "
        "Do not thank them for calling. Ask whether they want details, a brochure, "
        "or a site visit. "
        f"{LANGUAGE_ADAPTATION_INSTRUCTION}"
    )
    inbound_script = (
        "Inbound support/enquiry call. The lead called us. Thank them for calling, "
        "ask how you can help, then answer questions and qualify their interest. "
        "Do not say you are calling them about an enquiry. "
        f"{LANGUAGE_ADAPTATION_INSTRUCTION}"
    )
    unknown_inbound_script = (
        "New inbound caller. Their name is not in Zoho yet. Thank them for calling Soil Systems, "
        "introduce yourself as Vikas, ask for their name, city, and what details they need about "
        "the land project. Confirm their phone number if needed. Save the collected details in "
        "structured data using caller_name, caller_city, caller_email if shared, and caller_requirement. "
        f"{LANGUAGE_ADAPTATION_INSTRUCTION}"
    )
    call_script = (
        outbound_bridge_script
        if is_outbound_bridge
        else unknown_inbound_script if is_new_inbound_lead or clean_name.lower() == "unknown" else inbound_script
    )

    variables = {
        "lead_name": clean_name,
        "customer_name": clean_name,
        "name": clean_name,
        "agent_name": "Vikas",
        "phone": lead.phone,
        "language": "auto",
        "language_preference": "auto",
        "language_instruction": LANGUAGE_ADAPTATION_INSTRUCTION,
        "city": lead.city or "",
        "campaign": lead.campaign or "",
        "zoho_lead_id": lead.zoho_lead_id,
        "call_direction": call_direction,
        "inbound_call": "false" if is_outbound_bridge else "true",
        "outbound_bridge_call": "true" if is_outbound_bridge else "false",
        "call_context": call_direction,
        "call_script": call_script,
        "conversation_script": call_script,
        "opening_instruction": (
            "You placed this outbound callback call to the lead."
            if is_outbound_bridge
            else "This is a new inbound caller; collect their name and enquiry details."
            if is_new_inbound_lead or clean_name.lower() == "unknown"
            else "The lead called Soil Systems inbound."
        ),
        "caller_known": "false" if is_new_inbound_lead or clean_name.lower() == "unknown" else "true",
    }
    begin_message = (
        (
            f"Hello, am I speaking with {clean_name}? "
            "This is Vikas calling from Soil Systems about your land investment enquiry."
        )
        if is_outbound_bridge
        else (
            "Hi, thank you for calling Soil Systems. This is Vikas. "
            "May I know your name and what details you are looking for today?"
        )
        if is_new_inbound_lead or clean_name.lower() == "unknown"
        else (
            f"Hi {clean_name}, thank you for calling Soil Systems. "
            "This is Vikas. How can I help you today?"
        )
    )
    logger.info(
        "Retell inbound answer for lead_id=%s using call_direction=%s begin_message=%s",
        lead.id,
        call_direction,
        begin_message,
    )

    call_inbound_response = {
        "override_agent_id": settings.RETELL_AGENT_ID,
        "dynamic_variables": variables,
        "retell_llm_dynamic_variables": variables,
        "agent_override": {
            "retell_llm": {
                "begin_message": begin_message,
                "general_prompt": call_script,
            },
            "conversation_flow": {
                "begin_message": begin_message,
                "global_prompt": call_script,
            },
        },
        "metadata": {
            "lead_id": str(lead.id),
            "zoho_lead_id": lead.zoho_lead_id,
            "source": "leadcaller_retell_inbound",
            "call_direction": call_direction,
            "new_inbound_lead": is_new_inbound_lead,
        },
    }
    if settings.RETELL_AGENT_VERSION is not None:
        call_inbound_response["override_agent_version"] = settings.RETELL_AGENT_VERSION

    return JSONResponse(
        status_code=200,
        content={"call_inbound": call_inbound_response},
    )


async def _find_lead_for_retell_inbound(candidate_numbers: list[str | None], db: AsyncSession) -> Lead | None:
    searched_phone_number = False
    for raw_number in candidate_numbers:
        if not raw_number:
            continue
        digits = "".join(c for c in raw_number if c.isdigit())
        suffix = digits[-10:] if len(digits) >= 10 else digits
        if not suffix:
            continue
        searched_phone_number = True
        result = await db.execute(
            select(Lead).where((Lead.phone == raw_number) | (Lead.phone.like(f"%{suffix}"))).limit(1)
        )
        lead = result.scalars().first()
        if lead:
            return lead

    if searched_phone_number:
        return None

    result = await db.execute(
        select(CrmSyncLog)
        .options(selectinload(CrmSyncLog.lead))
        .where(CrmSyncLog.operation == "exotel_connect_call", CrmSyncLog.success.is_(True))
        .order_by(desc(CrmSyncLog.synced_at))
        .limit(1)
    )
    latest_exotel_log = result.scalar_one_or_none()
    return latest_exotel_log.lead if latest_exotel_log else None


async def _create_unknown_inbound_lead(caller_phone: str, db: AsyncSession) -> Lead:
    existing = await _find_lead_for_retell_inbound([caller_phone], db)
    if existing:
        return existing

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


async def _is_recent_exotel_outbound_for_lead(lead: Lead, db: AsyncSession) -> bool:
    window_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    result = await db.execute(
        select(CrmSyncLog)
        .where(CrmSyncLog.lead_id == lead.id)
        .where(CrmSyncLog.operation == "exotel_connect_call")
        .where(CrmSyncLog.success.is_(True))
        .where(CrmSyncLog.synced_at >= window_start)
        .order_by(desc(CrmSyncLog.synced_at))
        .limit(1)
    )
    if not hasattr(result, "scalar_one_or_none"):
        return False
    return result.scalar_one_or_none() is not None
