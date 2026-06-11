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
from app.call_scripts import (
    LANGUAGE_ADAPTATION_INSTRUCTION,
    OUTBOUND_SCRIPT,
    OUTBOUND_BEGIN_KNOWN,
    OUTBOUND_BEGIN_UNKNOWN,
    INBOUND_SCRIPT,
    INBOUND_BEGIN_KNOWN,
    INBOUND_UNKNOWN_SCRIPT,
    INBOUND_BEGIN_UNKNOWN,
)
from app.services.retell_service import (
    process_retell_completion,
    retell_event_key,
    schedule_retry,
    trigger_retell_call,
)
from app.services.exotel_service import pop_pending_outbound_bridge
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


@router.api_route("/exotel/exoml", methods=["GET", "POST"])
async def exotel_exoml(request: Request) -> Response:
    """Dynamic ExoML endpoint called by Exotel when the lead picks up.

    Returns ExoML that dials the Retell SIP number (+918046376848).
    Retell receives this as an inbound call and, because there is a recent
    'exotel_ai_bridge' CRM log for the lead, the /retell/inbound handler
    uses the outbound-bridge script so the AI starts speaking first.
    """
    settings = get_settings()

    # Log incoming data for debugging (GET uses query params, POST uses form data)
    try:
        if request.method == "GET":
            payload = dict(request.query_params)
        else:
            form = await request.form()
            payload = dict(form)
    except Exception:
        payload = {}
    lead_phone = (
        _payload_value(payload, "From", "CallFrom", "from") or ""
    )
    logger.info("[ExoML] %s request — lead picked up. Dialling Retell SIP. lead_phone=%s payload=%s",
                request.method, lead_phone, payload)

    # Dial the Retell SIP number — this uses the proven inbound path.
    # Retell will call /webhooks/retell/inbound and, seeing a recent outbound
    # bridge log, will run the AI in outbound mode.
    retell_sip_number = settings.RETELL_FROM_NUMBER  # +918046376848
    caller_id = settings.EXOTEL_CALLER_ID or ""       # 08047283246

    exoml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial callerId="{caller_id}">{retell_sip_number}</Dial>'
        "</Response>"
    )
    return Response(content=exoml, media_type="application/xml")


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
    custom_sip_headers = call_inbound.get("custom_sip_headers") or payload.get("custom_sip_headers") or {}
    settings = get_settings()
    exophone = (settings.EXOTEL_CALLER_ID or "").replace("-", "")

    # ── Outbound bridge identification via Call SID ──
    cached_lead = None
    db_lead = None
    is_outbound_bridge = False

    # Extract Call SID from custom SIP headers if present
    exotel_call_sid = None
    for k, v in custom_sip_headers.items():
        if k.lower() in {"x-exotel-callsid", "x-exotel-call-sid", "x-callsid", "callsid"}:
            exotel_call_sid = str(v)
            break

    from_number = call_inbound.get("from_number") or payload.get("from_number") or ""
    from_digits = "".join(c for c in from_number if c.isdigit())
    is_from_exophone = exophone and from_digits.endswith(exophone[-10:])

    # ── Outbound bridge identification ──
    # CORRECT MODEL: Both inbound AND outbound calls arrive at Retell with
    # from_number == ExoPhone (Exotel is always the SIP sender). So from_number
    # is NOT a discriminator.
    #
    # The ONLY reliable signal that a call is outbound is that WE registered it
    # in cache/DB when we initiated it via Exotel's /Calls/connect API. Any call
    # not present in our cache/DB is, by definition, an unsolicited inbound call.
    #
    # Strategy (in priority order):
    #   1. Cache hit by exact SID    → outbound
    #   2. DB hit by exact SID       → outbound
    #   3. LIFO cache (very recent)  → outbound (handles SID format mismatch)
    #   4. Most-recent-DB (5min)     → outbound (handles SID format mismatch)
    #   5. otherwise                 → inbound

    if exotel_call_sid:
        logger.info("Retell inbound: Extracted Exotel Call SID = %s", exotel_call_sid)
        cached_lead = pop_pending_outbound_bridge(exotel_call_sid)
        if cached_lead:
            logger.info("Retell inbound: Found cached outbound lead by Call SID: %s", cached_lead.get("lead_name"))
            is_outbound_bridge = True
        elif hasattr(db, "execute"):
            db_lead = await _find_recent_exotel_lead(db, exotel_call_sid=exotel_call_sid)
            if db_lead:
                logger.info("Retell inbound: Found recent outbound lead in DB by Call SID: %s", db_lead.name)
                is_outbound_bridge = True

    # SID-mismatch fallback: if we have ANY pending outbound bridge in cache (set
    # by us within the last 5 minutes), this almost certainly IS that call — the
    # SID format from Exotel /Calls/connect differs from what Retell sees in SIP.
    # Without this fallback, every outbound call would be mis-classified as inbound.
    if not is_outbound_bridge:
        cached_lead = pop_pending_outbound_bridge()  # LIFO
        if cached_lead:
            logger.info("Retell inbound: SID didn't match but LIFO cache has recent outbound lead: %s", cached_lead.get("lead_name"))
            is_outbound_bridge = True
        elif hasattr(db, "execute"):
            db_lead = await _find_recent_exotel_lead(db)
            if db_lead:
                logger.info("Retell inbound: SID didn't match but DB has recent outbound call for: %s", db_lead.name)
                is_outbound_bridge = True

    if not is_outbound_bridge:
        logger.info("Retell inbound: No outbound bridge registered — treating as customer inbound call")

    if is_outbound_bridge:
        # Outbound bridge — respond with lead data if we have it, generic outbound greeting otherwise
        if cached_lead:
            lead_id = cached_lead["lead_id"]
            lead_name = cached_lead["lead_name"]
            lead_phone = cached_lead["lead_phone"]
            city = cached_lead.get("city", "")
            campaign = cached_lead.get("campaign", "")
            source = cached_lead.get("source", "")
            zoho_lead_id = cached_lead.get("zoho_lead_id", "")
            language_pref = cached_lead.get("language_preference", "")
        elif db_lead:
            lead_id = str(db_lead.id)
            lead_name = db_lead.name
            lead_phone = db_lead.phone
            city = db_lead.city or ""
            campaign = db_lead.campaign or ""
            source = db_lead.source or ""
            zoho_lead_id = db_lead.zoho_lead_id
            language_pref = db_lead.language_preference.value if db_lead.language_preference else ""
        else:
            # Outbound bridge confirmed by ExoPhone but no lead found — use generic outbound greeting
            lead_id = ""
            lead_name = ""
            lead_phone = from_number
            city = ""
            campaign = ""
            source = ""
            zoho_lead_id = ""
            language_pref = ""

        clean_name = lead_name.replace("(Sample)", "").replace("(sample)", "").replace("Test", "").strip()
        return _build_inbound_response(
            settings=settings,
            lead_id=lead_id,
            lead_name=clean_name,
            lead_phone=lead_phone,
            city=city,
            campaign=campaign,
            source=source,
            zoho_lead_id=zoho_lead_id,
            is_outbound_bridge=True,
            is_new_inbound_lead=False,
            language_preference=language_pref,
        )

    # ── FAST PATH: Regular inbound call ──
    # Respond INSTANTLY so Retell never plays a "please wait" hold message.
    # The caller's phone is the only info we need; the AI agent already has the
    # full Vikas persona. Lead creation happens in the post-call webhook.
    caller_phone = call_inbound.get("from_number") or payload.get("from_number") or ""
    logger.info("Inbound call from %s — responding immediately (no DB lookup)", caller_phone)
    return _build_inbound_response(
        settings=settings,
        lead_id="",
        lead_name="",
        lead_phone=caller_phone,
        city="",
        campaign="",
        zoho_lead_id=None,
        is_outbound_bridge=False,
        is_new_inbound_lead=True,
    )


RETELL_LANGUAGE_MAP = {
    "english": "en-IN",
    "hindi": "hi-IN",
    "kannada": "kn-IN",
}


def _build_inbound_response(
    *,
    settings: Any,
    lead_id: str,
    lead_name: str,
    lead_phone: str,
    city: str,
    campaign: str,
    zoho_lead_id: str | None,
    is_outbound_bridge: bool,
    is_new_inbound_lead: bool,
    language_preference: str = "",
    source: str = "",
) -> JSONResponse:
    """Build the Retell inbound webhook response. Pure function — no DB access."""
    call_direction = "outbound" if is_outbound_bridge else "inbound"
    retell_language = RETELL_LANGUAGE_MAP.get(language_preference.lower(), "en-IN")

    if is_outbound_bridge:
        call_script = OUTBOUND_SCRIPT
    elif is_new_inbound_lead or lead_name.lower() == "unknown":
        call_script = INBOUND_UNKNOWN_SCRIPT
    else:
        call_script = INBOUND_SCRIPT

    # Build a human-readable source label so the agent can say "you enquired via Instagram"
    # Check both source and campaign fields — Zoho may store "Instagram" in either
    source_label = ""
    combined_lower = f"{source or ''} {campaign or ''}".lower()
    if "instagram" in combined_lower or "ig " in combined_lower:
        source_label = "Instagram"
    elif "facebook" in combined_lower or "fb " in combined_lower:
        source_label = "Facebook"
    elif "google" in combined_lower:
        source_label = "Google"
    elif source:
        source_label = source
    elif campaign:
        source_label = campaign

    variables = {
        "lead_name": lead_name,
        "customer_name": lead_name,
        "name": lead_name,
        "agent_name": "Vikas",
        "phone": lead_phone,
        "language": "auto",
        "language_preference": "auto",
        "language_instruction": LANGUAGE_ADAPTATION_INSTRUCTION,
        "city": city,
        "campaign": campaign,
        "lead_source": source_label,
        "zoho_lead_id": zoho_lead_id or "",
        "call_direction": call_direction,
        "inbound_call": "false" if is_outbound_bridge else "true",
        "outbound_bridge_call": "true" if is_outbound_bridge else "false",
        "call_context": call_direction,
        "call_script": call_script,
        "conversation_script": call_script,
        "opening_instruction": (
            (
                f"You placed this outbound call to {lead_name}. "
                + (f"They showed interest via {source_label}. " if source_label else "")
                + "You have already introduced yourself in your first message — "
                "do NOT introduce yourself again. Continue the conversation naturally."
            )
            if is_outbound_bridge
            else "This is a new inbound caller; collect their name and enquiry details."
            if is_new_inbound_lead or lead_name.lower() == "unknown"
            else "The lead called Soil Systems inbound."
        ),
        "caller_known": "false" if is_new_inbound_lead or lead_name.lower() == "unknown" else "true",
    }

    # Always override begin_message so the greeting matches the call direction.
    # The agent's default begin_message on Retell is outbound-style, so inbound
    # calls MUST also be overridden to avoid using the wrong script.
    if is_outbound_bridge:
        if not lead_name or lead_name.lower() == "unknown":
            begin_message = OUTBOUND_BEGIN_UNKNOWN
        else:
            begin_message = OUTBOUND_BEGIN_KNOWN.format(lead_name=lead_name)
    elif is_new_inbound_lead or not lead_name or lead_name.lower() == "unknown":
        begin_message = INBOUND_BEGIN_UNKNOWN
    else:
        begin_message = INBOUND_BEGIN_KNOWN.format(lead_name=lead_name)

    logger.info(
        "Retell inbound answer for lead_id=%s call_direction=%s begin_message=%s",
        lead_id,
        call_direction,
        begin_message,
    )

    # Override ONLY the begin_message. Do NOT override general_prompt — the
    # Retell dashboard prompt is the source of truth for agent persona and
    # branches on {{call_direction}} / dynamic variables we already send.
    # Overriding general_prompt would wipe out the dashboard's rich prompt.
    agent_override: dict[str, Any] = {
        "agent": {
            "language": retell_language,
        },
        "retell_llm": {
            "begin_message": begin_message,
        },
        "conversation_flow": {
            "begin_message": begin_message,
        },
    }

    call_inbound_response: dict[str, Any] = {
        "override_agent_id": settings.RETELL_INBOUND_AGENT_ID if (not is_outbound_bridge and getattr(settings, "RETELL_INBOUND_AGENT_ID", None)) else settings.RETELL_AGENT_ID,
        "dynamic_variables": variables,
        "retell_llm_dynamic_variables": variables,
        "agent_override": agent_override,
        "metadata": {
            "lead_id": lead_id,
            "zoho_lead_id": zoho_lead_id or "",
            "source": "leadcaller_retell_inbound",
            "call_direction": call_direction,
            "new_inbound_lead": is_new_inbound_lead,
        },
    }

    return JSONResponse(
        status_code=200,
        content={"call_inbound": call_inbound_response},
    )


async def _find_lead_for_retell_inbound(
    candidate_numbers: list[str | None],
    db: AsyncSession,
    exotel_call_sid: str | None = None,
) -> Lead | None:
    exotel_lead = await _find_recent_exotel_lead(db, exotel_call_sid=exotel_call_sid)
    if exotel_lead:
        return exotel_lead

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

    return None if searched_phone_number else await _find_recent_exotel_lead(db)


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


@router.post("/whatsapp/status")
async def whatsapp_status_callback(request: Request) -> JSONResponse:
    """Capture Exotel WhatsApp delivery status callbacks for debugging."""
    body = await request.json()
    logger.info("[WhatsApp] Delivery status callback: %s", json.dumps(body, indent=2))
    return JSONResponse(status_code=200, content={"status": "received"})


async def _find_recent_exotel_lead(db: AsyncSession, exotel_call_sid: str | None = None) -> Lead | None:
    # Tight 90-second window matches the in-memory cache TTL — bridged outbound
    # calls reach Retell's inbound webhook within ~10-30s of Exotel.connect.
    # A wider window risks misclassifying a real customer-inbound call as an
    # outbound bridge based on a stale CrmSyncLog row.
    window_start = datetime.now(timezone.utc) - timedelta(seconds=90)
    query = (
        select(CrmSyncLog)
        .options(selectinload(CrmSyncLog.lead))
        .where(CrmSyncLog.operation == "exotel_connect_call")
        .where(CrmSyncLog.success.is_(True))
        .where(CrmSyncLog.synced_at >= window_start)
        .order_by(desc(CrmSyncLog.synced_at))
    )
    if exotel_call_sid:
        query = query.where(CrmSyncLog.error_message.contains(str(exotel_call_sid)))

    result = await db.execute(query.limit(1))
    if not hasattr(result, "scalar_one_or_none"):
        return None

    latest_exotel_log = result.scalar_one_or_none()
    return latest_exotel_log.lead if latest_exotel_log else None


async def _is_recent_exotel_outbound_for_lead(
    lead: Lead,
    db: AsyncSession,
    exotel_call_sid: str | None = None,
) -> bool:
    window_start = datetime.now(timezone.utc) - timedelta(minutes=5)
    query = (
        select(CrmSyncLog)
        .where(CrmSyncLog.lead_id == lead.id)
        .where(CrmSyncLog.operation == "exotel_connect_call")
        .where(CrmSyncLog.success.is_(True))
        .where(CrmSyncLog.synced_at >= window_start)
        .order_by(desc(CrmSyncLog.synced_at))
    )
    if exotel_call_sid:
        query = query.where(CrmSyncLog.error_message.contains(str(exotel_call_sid)))

    result = await db.execute(query.limit(1))
    if not hasattr(result, "scalar_one_or_none"):
        return False
    return result.scalar_one_or_none() is not None
