"""API Router for manual Exotel WhatsApp automation operations from the LeadCaller dashboard.
"""

import json
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import (
    WebhookEvent,
    WebhookSource,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageType,
)
from app.models.lead import Lead
from app.services import exotel_whatsapp_service
from app.services.whatsapp_service import (
    format_phone_for_whatsapp,
    send_whatsapp_call_completed,
    send_whatsapp_call_missed,
    send_whatsapp_custom,
)

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_SIZE = 25 * 1024 * 1024  # 25 MB

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

WEBHOOK_VERIFY_TOKEN = "leadcaller_webhook_2024"


class CustomMessageRequest(BaseModel):
    phone: str = Field(..., description="Recipient phone number with country code, e.g. +91XXXXXXXXXX")
    text: str = Field(..., description="Message body text")


class TemplateMessageRequest(BaseModel):
    phone: str = Field(..., description="Recipient phone number with country code, e.g. +91XXXXXXXXXX")
    lead_name: str = Field(..., description="Name of the lead")
    template_type: Literal["completed", "missed"] = Field(..., description="Template type to send")


class ManualNudgeRequest(BaseModel):
    lead_id: uuid.UUID = Field(..., description="ID of the lead in database")
    nudge_type: Literal["completed", "missed"] = Field(..., description="Completed call followup or missed call nudge")


@router.post("/send-custom")
async def send_custom_wa(
    payload: CustomMessageRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Send an arbitrary free-text message to a number (works within the 24h active session window)."""
    background_tasks.add_task(send_whatsapp_custom, payload.phone, payload.text)
    return JSONResponse(
        status_code=200,
        content={"status": "enqueued", "message": "Custom WhatsApp message sending enqueued."}
    )


@router.post("/send-template")
async def send_template_wa(
    payload: TemplateMessageRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Send a pre-approved template message to a phone number manually."""
    if payload.template_type == "completed":
        background_tasks.add_task(send_whatsapp_call_completed, payload.lead_name, payload.phone)
    else:
        background_tasks.add_task(send_whatsapp_call_missed, payload.lead_name, payload.phone)

    return JSONResponse(
        status_code=200,
        content={"status": "enqueued", "message": f"{payload.template_type.capitalize()} template enqueued."}
    )


@router.post("/send-nudge")
async def send_nudge_wa(
    payload: ManualNudgeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Manually trigger a WhatsApp follow-up or missed call nudge for a specific lead."""
    lead = await db.get(Lead, payload.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    phone = lead.phone
    name = lead.name or "Lead"

    if payload.nudge_type == "completed":
        logger.info("Manual WhatsApp trigger (completed) enqueued for lead_id=%s phone=%s name=%s", lead.id, phone, name)
        background_tasks.add_task(send_whatsapp_call_completed, name, phone)
    else:
        logger.info("Manual WhatsApp trigger (missed) enqueued for lead_id=%s phone=%s name=%s", lead.id, phone, name)
        background_tasks.add_task(send_whatsapp_call_missed, name, phone)

    return JSONResponse(
        status_code=200,
        content={
            "status": "enqueued",
            "message": f"Nudge type '{payload.nudge_type}' successfully enqueued for lead '{name}'.",
            "phone": phone
        }
    )


def _extract_whatsapp_payload(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Extract sender / text / type / timestamp / message_id from any Exotel inbound shape.

    Real Exotel inbound shape:
      {"whatsapp": {"messages": [{
          "from": "+91...",
          "to": "+91...",
          "sid": "abc...",
          "content": {"type": "text", "text": {"body": "Hi"}},
          "timestamp": "...",
          "profile_name": "...",
          "callback_type": "incoming_message"  # or "dlr" for delivery receipts
      }]}}
    Test/older shapes may put data flat at the top level.
    """
    msg: dict[str, Any] = {}
    wa = payload.get("whatsapp")
    if isinstance(wa, dict):
        msgs = wa.get("messages")
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
            msg = msgs[0]
    if not msg and isinstance(payload.get("message"), dict):
        msg = payload["message"]

    content = msg.get("content") if isinstance(msg.get("content"), dict) else {}

    def pick(*keys: str, src: dict[str, Any] | None = None) -> Any:
        target = src if src is not None else payload
        for k in keys:
            v = target.get(k)
            if v not in (None, ""):
                return v
        return None

    sender = (
        pick("from", "sender", "sender_phone", "from_number", src=msg)
        or pick("from", "sender", "sender_phone", "from_number")
    )
    message_id = (
        pick("sid", "id", "message_id", "msg_id", src=msg)
        or pick("message_id", "id", "msg_id", "sid")
    )
    timestamp = (
        pick("timestamp", "sent_at", "time", src=msg)
        or pick("timestamp", "sent_at", "time")
    )

    # Type: msg.content.type (real Exotel), then msg.type, then top-level
    message_type = (
        pick("type", src=content)
        or pick("type", "message_type", "content_type", src=msg)
        or pick("type", "message_type", "content_type")
    )

    # Text body: content.text.body (real Exotel) → msg.text.body → top-level body
    message_text: str | None = None
    for source in (content, msg, payload):
        if not isinstance(source, dict):
            continue
        text_obj = source.get("text")
        if isinstance(text_obj, dict) and text_obj.get("body"):
            message_text = text_obj["body"]
            break
        if isinstance(text_obj, str) and text_obj:
            message_text = text_obj
            break
    if not message_text:
        message_text = (
            pick("body", "message", "message_body", src=msg)
            or pick("body", "message", "message_body")
        )

    # Infer type from media node if still missing
    if not message_type:
        for k in ("text", "image", "document", "video", "audio", "location", "interactive", "template"):
            if isinstance(content.get(k), dict) or isinstance(msg.get(k), dict):
                message_type = k
                break

    return sender, message_text, message_type, timestamp, message_id


def _validate_webhook_token(payload: dict[str, Any], request: Request) -> None:
    """
    Verify the inbound webhook came from a trusted source.

    Exotel WhatsApp webhooks don't include a verify_token, so we accept calls
    that omit one (security comes from the unguessable webhook URL). When a
    token IS provided (e.g., from Meta-style verification), we still validate
    it to keep that path strict.
    """
    token = payload.get("verify_token") or request.query_params.get("verify_token")
    if token is None:
        return  # Exotel-style: no token present, trust the URL secrecy
    if token != WEBHOOK_VERIFY_TOKEN:
        raise HTTPException(status_code=401, detail="invalid verify token")


@router.post("/webhook")
async def whatsapp_incoming_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:
        logger.error("[WhatsApp] Invalid JSON payload: %s", exc)
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc

    _validate_webhook_token(payload, request)
    sender, message_text, message_type, timestamp, message_id = _extract_whatsapp_payload(payload)

    logger.info("[WhatsApp] Incoming message payload: %s", json.dumps(payload, indent=2))
    logger.info(
        "[WhatsApp] Parsed incoming message sender=%s message_id=%s type=%s timestamp=%s text=%s",
        sender,
        message_id,
        message_type,
        timestamp,
        message_text,
    )

    # Store incoming WhatsApp message as a WebhookEvent in the database.
    idempotency_key = message_id or f"wa_msg:{uuid.uuid4()}"
    
    # Check if we already received this message
    existing_event = (
        await db.execute(
            select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    
    # Detect Exotel delivery-receipt (DLR) callbacks routed to the inbound URL.
    # DLR rows have callback_type: "dlr" and no `from` — they should be logged
    # as webhook events but never inserted as chat messages.
    is_dlr = False
    wa_node = payload.get("whatsapp")
    if isinstance(wa_node, dict):
        msgs = wa_node.get("messages")
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
            is_dlr = msgs[0].get("callback_type") == "dlr"

    if not existing_event:
        webhook_event = WebhookEvent(
            source=WebhookSource.whatsapp,
            event_type="delivery_receipt" if is_dlr else "incoming_message",
            payload=payload,
            processed=True,
            idempotency_key=idempotency_key,
        )
        db.add(webhook_event)
        # Also persist a flat message row for the chat UI (skip DLRs)
        if sender and not is_dlr:
            msg_type, body, media_url, media_filename, media_caption, lat, lon, loc_name = _parse_inbound_content(payload)
            normalized_phone = format_phone_for_whatsapp(sender) or sender
            chat_msg = WhatsAppMessage(
                phone=normalized_phone,
                direction=WhatsAppMessageDirection.inbound,
                message_type=msg_type,
                body=body or message_text,
                media_url=media_url,
                media_filename=media_filename,
                media_caption=media_caption,
                latitude=lat,
                longitude=lon,
                location_name=loc_name,
                provider_message_id=message_id,
                raw_payload=payload,
            )
            db.add(chat_msg)
        await db.commit()
        logger.info("[WhatsApp] Stored webhook event: idempotency_key=%s", idempotency_key)
    else:
        logger.info("[WhatsApp] Duplicate webhook event: idempotency_key=%s", idempotency_key)

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "message": "WhatsApp message received",
            "message_id": message_id,
        },
    )


def _parse_inbound_content(payload: dict[str, Any]) -> tuple[WhatsAppMessageType, str | None, str | None, str | None, str | None, str | None, str | None, str | None]:
    """Extract structured fields from an Exotel inbound payload.

    Real Exotel shape:  payload["whatsapp"]["messages"][0]["content"]["{text|image|...}"]
    Test shape:         flat at payload root
    Returns: (message_type, body, media_url, media_filename, media_caption, lat, lon, location_name)
    """
    # Build a list of source dicts to scan, in priority order (highest first)
    sources: list[dict[str, Any]] = []
    # Real Exotel: dig into whatsapp.messages[0].content first, then the message itself
    wa = payload.get("whatsapp")
    if isinstance(wa, dict):
        msgs = wa.get("messages")
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
            m0 = msgs[0]
            c0 = m0.get("content")
            if isinstance(c0, dict):
                sources.append(c0)
            sources.append(m0)
    msg_node = payload.get("message")
    if isinstance(msg_node, dict):
        c1 = msg_node.get("content")
        if isinstance(c1, dict):
            sources.append(c1)
        sources.append(msg_node)
    content_root = payload.get("content")
    if isinstance(content_root, dict):
        sources.append(content_root)
    sources.append(payload)

    raw_type = ""
    for src in sources:
        t = src.get("type") or src.get("message_type")
        if t:
            raw_type = str(t).lower()
            break

    body: str | None = None
    for src in sources:
        text = src.get("text")
        if isinstance(text, dict) and text.get("body"):
            body = text["body"]
            break
        if isinstance(text, str):
            body = text
            break
    if not body:
        for src in sources:
            v = src.get("body") or src.get("message") or src.get("message_body")
            if v:
                body = v
                break

    media_url = media_filename = media_caption = None
    for key in ("image", "document", "video", "audio"):
        for src in sources:
            node = src.get(key)
            if isinstance(node, dict):
                media_url = media_url or node.get("link") or node.get("url")
                media_filename = media_filename or node.get("filename")
                media_caption = media_caption or node.get("caption")
                if not raw_type:
                    raw_type = key

    lat = lon = loc_name = None
    for src in sources:
        loc = src.get("location")
        if isinstance(loc, dict):
            lat = str(loc.get("latitude")) if loc.get("latitude") is not None else None
            lon = str(loc.get("longitude")) if loc.get("longitude") is not None else None
            loc_name = loc.get("name")
            if not raw_type:
                raw_type = "location"
            break

    try:
        msg_type = WhatsAppMessageType(raw_type)
    except ValueError:
        msg_type = WhatsAppMessageType.text if body else WhatsAppMessageType.other

    return msg_type, body, media_url, media_filename, media_caption, lat, lon, loc_name


# ── Two-way chat: conversation list + thread + send + upload ──


class SendMessageRequest(BaseModel):
    type: Literal["text", "image", "document", "video", "audio", "location"] = "text"
    body: str | None = None
    media_url: str | None = None
    media_filename: str | None = None
    caption: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    location_address: str | None = None


def _message_to_dict(m: WhatsAppMessage) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "phone": m.phone,
        "direction": m.direction.value if hasattr(m.direction, "value") else str(m.direction),
        "type": m.message_type.value if hasattr(m.message_type, "value") else str(m.message_type),
        "body": m.body,
        "media_url": m.media_url,
        "media_filename": m.media_filename,
        "caption": m.media_caption,
        "latitude": m.latitude,
        "longitude": m.longitude,
        "location_name": m.location_name,
        "provider_message_id": m.provider_message_id,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/conversations")
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List distinct phone numbers with last-message preview, newest first."""
    limit = min(max(limit, 1), 500)
    subq = (
        select(
            WhatsAppMessage.phone,
            func.max(WhatsAppMessage.created_at).label("last_at"),
        )
        .group_by(WhatsAppMessage.phone)
        .order_by(desc("last_at"))
        .limit(limit)
        .subquery()
    )
    result = await db.execute(select(subq.c.phone, subq.c.last_at))
    rows = result.all()

    out: list[dict[str, Any]] = []
    for phone, last_at in rows:
        latest = await db.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.phone == phone)
            .order_by(desc(WhatsAppMessage.created_at))
            .limit(1)
        )
        msg = latest.scalar_one_or_none()
        if not msg:
            continue
        # Try to attach a lead name if we have one
        lead_result = await db.execute(
            select(Lead).where(Lead.phone.like(f"%{phone[-10:]}")).limit(1)
        )
        lead = lead_result.scalar_one_or_none()
        out.append(
            {
                "phone": phone,
                "lead_name": lead.name if lead else None,
                "last_message": _message_to_dict(msg),
                "last_at": last_at.isoformat() if last_at else None,
            }
        )
    return out


@router.get("/conversations/{phone}")
async def get_conversation(
    phone: str,
    db: AsyncSession = Depends(get_db),
    limit: int = 200,
) -> dict[str, Any]:
    """Return full message thread for a phone number (chronological)."""
    limit = min(max(limit, 1), 1000)
    normalized = format_phone_for_whatsapp(phone) or phone
    suffix = "".join(c for c in normalized if c.isdigit())[-10:]
    result = await db.execute(
        select(WhatsAppMessage)
        .where(WhatsAppMessage.phone.like(f"%{suffix}"))
        .order_by(WhatsAppMessage.created_at.asc())
        .limit(limit)
    )
    messages = [_message_to_dict(m) for m in result.scalars()]
    lead_result = await db.execute(select(Lead).where(Lead.phone.like(f"%{suffix}")).limit(1))
    lead = lead_result.scalar_one_or_none()
    return {
        "phone": normalized,
        "lead_name": lead.name if lead else None,
        "lead_id": str(lead.id) if lead else None,
        "messages": messages,
    }


@router.post("/conversations/{phone}/send")
async def send_to_conversation(
    phone: str,
    payload: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Send a WhatsApp message via Exotel and persist it to the conversation."""
    normalized = format_phone_for_whatsapp(phone)
    if not normalized:
        raise HTTPException(status_code=400, detail=f"invalid phone: {phone}")

    msg_type_map = {
        "text": WhatsAppMessageType.text,
        "image": WhatsAppMessageType.image,
        "document": WhatsAppMessageType.document,
        "video": WhatsAppMessageType.video,
        "audio": WhatsAppMessageType.audio,
        "location": WhatsAppMessageType.location,
    }

    response: dict[str, Any]
    if payload.type == "text":
        if not payload.body:
            raise HTTPException(status_code=400, detail="body required for text message")
        response = await exotel_whatsapp_service.send_text(normalized, payload.body)
    elif payload.type == "image":
        if not payload.media_url:
            raise HTTPException(status_code=400, detail="media_url required for image")
        response = await exotel_whatsapp_service.send_image(normalized, payload.media_url, payload.caption)
    elif payload.type == "document":
        if not payload.media_url or not payload.media_filename:
            raise HTTPException(status_code=400, detail="media_url and media_filename required for document")
        response = await exotel_whatsapp_service.send_document(normalized, payload.media_url, payload.media_filename, payload.caption)
    elif payload.type == "video":
        if not payload.media_url:
            raise HTTPException(status_code=400, detail="media_url required for video")
        response = await exotel_whatsapp_service.send_video(normalized, payload.media_url, payload.caption)
    elif payload.type == "audio":
        if not payload.media_url:
            raise HTTPException(status_code=400, detail="media_url required for audio")
        response = await exotel_whatsapp_service.send_audio(normalized, payload.media_url)
    elif payload.type == "location":
        if payload.latitude is None or payload.longitude is None:
            raise HTTPException(status_code=400, detail="latitude and longitude required for location")
        response = await exotel_whatsapp_service.send_location(
            normalized, payload.latitude, payload.longitude, payload.location_name, payload.location_address
        )
    else:
        raise HTTPException(status_code=400, detail=f"unsupported type: {payload.type}")

    provider_id: str | None = None
    if isinstance(response, dict):
        # Exotel returns: response.response.whatsapp.messages[0].data.sid
        try:
            inner = response.get("response", {})
            if isinstance(inner, dict):
                wa = inner.get("whatsapp", {})
                if isinstance(wa, dict):
                    msgs = wa.get("messages", [])
                    if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
                        data = msgs[0].get("data", {})
                        if isinstance(data, dict):
                            provider_id = data.get("sid") or data.get("message_id") or data.get("id")
            # Fallbacks for simpler shapes
            if not provider_id:
                provider_id = response.get("message_id") or response.get("sid")
        except Exception:
            provider_id = None

    msg = WhatsAppMessage(
        phone=normalized,
        direction=WhatsAppMessageDirection.outbound,
        message_type=msg_type_map[payload.type],
        body=payload.body if payload.type == "text" else None,
        media_url=payload.media_url,
        media_filename=payload.media_filename,
        media_caption=payload.caption,
        latitude=str(payload.latitude) if payload.latitude is not None else None,
        longitude=str(payload.longitude) if payload.longitude is not None else None,
        location_name=payload.location_name,
        provider_message_id=provider_id,
        raw_payload={"request": payload.model_dump(), "response": response},
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return {"status": "sent", "message": _message_to_dict(msg), "provider_response": response}


@router.post("/upload")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Upload a file to /uploads and return a publicly-accessible URL Exotel can fetch."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    safe_name = f"{secrets.token_urlsafe(16)}{ext}"
    dest = UPLOAD_DIR / safe_name

    written = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await file.read(1 << 16)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_SIZE:
                dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"file too large (>{MAX_UPLOAD_SIZE} bytes)")
            fh.write(chunk)

    settings = get_settings()
    base = (settings.BASE_URL or str(request.base_url)).rstrip("/")
    public_url = f"{base}/uploads/{safe_name}"
    return {
        "url": public_url,
        "filename": file.filename,
        "stored_as": safe_name,
        "size": written,
        "content_type": file.content_type,
    }


@router.post("/webhook/status")
async def whatsapp_status_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:
        logger.error("[WhatsApp] Invalid JSON payload for status callback: %s", exc)
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc

    _validate_webhook_token(payload, request)
    sender, _, message_type, timestamp, message_id = _extract_whatsapp_payload(payload)
    status = payload.get("status") or payload.get("delivery_status") or payload.get("message_status")

    logger.info("[WhatsApp] Delivery status payload: %s", json.dumps(payload, indent=2))
    logger.info(
        "[WhatsApp] Parsed delivery status message_id=%s sender=%s status=%s type=%s timestamp=%s",
        message_id,
        sender,
        status,
        message_type,
        timestamp,
    )

    # Store incoming WhatsApp status as a WebhookEvent in the database.
    # To avoid key collisions with the message itself, prefix the idempotency key.
    idempotency_key = f"status:{message_id}:{status}" if message_id and status else f"wa_status:{uuid.uuid4()}"

    existing_event = (
        await db.execute(
            select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()

    if not existing_event:
        webhook_event = WebhookEvent(
            source=WebhookSource.whatsapp,
            event_type=f"status_{status or 'unknown'}",
            payload=payload,
            processed=True,
            idempotency_key=idempotency_key,
        )
        db.add(webhook_event)
        await db.commit()
        logger.info("[WhatsApp] Stored status webhook event: idempotency_key=%s", idempotency_key)
    else:
        logger.info("[WhatsApp] Duplicate status webhook event: idempotency_key=%s", idempotency_key)

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "message": "WhatsApp status callback received",
            "message_id": message_id,
        },
    )


@router.get("/health")
async def whatsapp_health() -> dict[str, str]:
    return {"status": "ok", "service": "whatsapp"}
