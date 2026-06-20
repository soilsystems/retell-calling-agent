"""Exotel WhatsApp send-message API.

POST https://<api_key>:<api_token>@<subdomain>/v2/accounts/<account_sid>/messages

Supports text, image, document, video, audio, location, template.
"""

import logging
import uuid as uuid_lib
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import (
    CallAttempt,
    CallJob,
    Lead,
    WhatsAppMessage,
    WhatsAppMessageDirection,
    WhatsAppMessageType,
)
from app.services.whatsapp_service import format_phone_for_whatsapp

logger = logging.getLogger(__name__)


def _exotel_url() -> str:
    """Build the Exotel send-message URL.

    Prefers the WhatsApp-specific credentials (EXOTEL_WA_*) which are tied to
    the WABA account. Falls back to the generic Exotel API keys for backwards
    compatibility with the earlier setup.
    """
    settings = get_settings()
    api_key = settings.EXOTEL_WA_API_KEY or settings.EXOTEL_API_KEY
    api_token = settings.EXOTEL_WA_API_TOKEN or settings.EXOTEL_API_TOKEN
    account_sid = settings.EXOTEL_WA_ACCOUNT_SID or settings.EXOTEL_ACCOUNT_SID
    subdomain = (
        (settings.EXOTEL_WA_SUBDOMAIN if settings.EXOTEL_WA_API_KEY else None)
        or settings.EXOTEL_SUBDOMAIN
        or "api.exotel.com"
    ).strip()
    if not (api_key and api_token and account_sid):
        raise HTTPException(
            status_code=500,
            detail="Exotel WhatsApp send requires EXOTEL_WA_API_KEY/TOKEN/ACCOUNT_SID (or EXOTEL_API_* fallback)",
        )
    return f"https://{api_key}:{api_token}@{subdomain}/v2/accounts/{account_sid}/messages"


def _from_number() -> str:
    settings = get_settings()
    src = (
        settings.EXOTEL_WA_PHONE_NUMBER
        or settings.EXOTEL_WHATSAPP_FROM_NUMBER
        or settings.EXOTEL_WHATSAPP_NUMBER
    )
    if not src:
        raise HTTPException(status_code=500, detail="EXOTEL_WA_PHONE_NUMBER not configured")
    return src


async def _post(payload: dict[str, Any]) -> dict[str, Any]:
    url = _exotel_url()
    # Wrap in {"whatsapp": {"messages": [...]}} envelope per Exotel v2 unified messaging API.
    # The flat {from, to, content} payload returns error 1001 "at least one channel is mandatory".
    wrapped = {"whatsapp": {"messages": [payload]}}
    msg_type = payload.get("content", {}).get("type")
    logger.info("[ExotelWA] POST %s msg-type=%s", url.split("@")[-1], msg_type)
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, json=wrapped, headers={"Content-Type": "application/json"})
    body: dict[str, Any]
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text}
    # Exotel returns HTTP 200 even when the inner message fails — check inner status too
    inner_messages = (body.get("whatsapp") or {}).get("messages") or []
    inner_failed = any(
        isinstance(m, dict) and m.get("status") == "failure"
        for m in inner_messages
    )
    if r.status_code >= 400 or inner_failed:
        logger.warning("[ExotelWA] failed status=%s body=%s", r.status_code, body)
        raise HTTPException(status_code=502, detail={"exotel_status": r.status_code, "exotel_body": body})
    logger.info("[ExotelWA] success status=%s", r.status_code)
    return body


def _envelope(to: str, content: dict[str, Any]) -> dict[str, Any]:
    return {"from": _from_number(), "to": to, "content": content}


def _normalize_to(to: str) -> str:
    normalized = format_phone_for_whatsapp(to)
    if not normalized:
        raise HTTPException(status_code=400, detail=f"invalid phone: {to}")
    return normalized


async def send_text(to: str, body: str) -> dict[str, Any]:
    return await _post(
        _envelope(
            _normalize_to(to),
            {"recipient_type": "individual", "type": "text", "text": {"body": body}},
        )
    )


async def send_image(to: str, link: str, caption: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"link": link}
    if caption:
        payload["caption"] = caption
    return await _post(
        _envelope(_normalize_to(to), {"type": "image", "image": payload})
    )


async def send_document(to: str, link: str, filename: str, caption: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"link": link, "filename": filename}
    if caption:
        payload["caption"] = caption
    return await _post(
        _envelope(_normalize_to(to), {"type": "document", "document": payload})
    )


async def send_video(to: str, link: str, caption: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"link": link}
    if caption:
        payload["caption"] = caption
    return await _post(
        _envelope(_normalize_to(to), {"type": "video", "video": payload})
    )


async def send_audio(to: str, link: str) -> dict[str, Any]:
    return await _post(
        _envelope(_normalize_to(to), {"type": "audio", "audio": {"link": link}})
    )


async def send_location(
    to: str,
    latitude: float,
    longitude: float,
    name: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    loc: dict[str, Any] = {"latitude": latitude, "longitude": longitude}
    if name:
        loc["name"] = name
    if address:
        loc["address"] = address
    return await _post(_envelope(_normalize_to(to), {"type": "location", "location": loc}))


async def send_template(
    to: str,
    name: str,
    language: str = "en",
    body_params: list[str] | None = None,
) -> dict[str, Any]:
    """Send a pre-approved WhatsApp template via Exotel.

    Templates bypass the 24-hour conversation window restriction. The template
    must already be approved in your Exotel WhatsApp dashboard.
    """
    template: dict[str, Any] = {
        "name": name,
        "language": {"code": language},
    }
    if body_params:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in body_params],
            }
        ]
    return await _post(
        _envelope(_normalize_to(to), {"type": "template", "template": template})
    )


def _extract_provider_sid(response: dict[str, Any]) -> str | None:
    """Dig the message sid out of Exotel's nested response."""
    try:
        inner = response.get("response") or response
        if isinstance(inner, dict):
            wa = inner.get("whatsapp", {})
            msgs = wa.get("messages", []) if isinstance(wa, dict) else []
            if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
                data = msgs[0].get("data", {})
                if isinstance(data, dict):
                    return data.get("sid") or data.get("message_id") or data.get("id")
    except Exception:
        pass
    return None


async def _log_outbound_template(
    db: AsyncSession,
    *,
    phone: str,
    template_name: str,
    response: dict[str, Any] | None,
    error: str | None,
) -> None:
    """Persist a row in whatsapp_messages so the sent template appears in chat UI."""
    body = f"📋 Template: {template_name}"
    if error:
        body += f"\n(failed: {error})"
    provider_id = _extract_provider_sid(response) if response else None
    msg = WhatsAppMessage(
        phone=phone,
        direction=WhatsAppMessageDirection.outbound,
        message_type=WhatsAppMessageType.template,
        body=body,
        provider_message_id=provider_id,
        raw_payload={"template": template_name, "response": response, "error": error},
    )
    db.add(msg)
    await db.commit()


async def send_post_call_template(
    call_attempt_id: uuid_lib.UUID,
    db: AsyncSession | None = None,
) -> None:
    """Send the post-call WhatsApp template to a lead after a call ends.

    Triggered from the Retell call_completed webhook. Always logs entry/exit so
    silent failures are visible in uvicorn logs. Always writes a row to
    whatsapp_messages — success or failure — so the chat UI reflects what
    happened.
    """
    logger.info("[ExotelWA] post-call template task START for attempt=%s", call_attempt_id)
    if db is None:
        try:
            async with AsyncSessionLocal() as session:
                await send_post_call_template(call_attempt_id, session)
        except Exception as exc:
            logger.exception("[ExotelWA] post-call template task CRASHED at session level: %s", exc)
        return

    settings = get_settings()
    template_name = settings.EXOTEL_WA_TEMPLATE_POST_CALL
    language = settings.EXOTEL_WA_TEMPLATE_POST_CALL_LANG
    logger.info("[ExotelWA] using template=%s language=%s", template_name, language)

    # Load the attempt and lead
    try:
        result = await db.execute(
            select(CallAttempt)
            .options(selectinload(CallAttempt.call_job).selectinload(CallJob.lead))
            .where(CallAttempt.id == call_attempt_id)
        )
        attempt = result.scalar_one_or_none()
    except Exception as exc:
        logger.exception("[ExotelWA] post-call template DB lookup failed: %s", exc)
        return

    if not attempt:
        logger.warning("[ExotelWA] post-call template skipped: CallAttempt %s not found", call_attempt_id)
        return
    if not attempt.call_job:
        logger.warning("[ExotelWA] post-call template skipped: CallAttempt %s has no call_job", call_attempt_id)
        return
    if not attempt.call_job.lead:
        logger.warning("[ExotelWA] post-call template skipped: CallJob has no lead")
        return

    lead: Lead = attempt.call_job.lead
    phone = format_phone_for_whatsapp(lead.phone)
    if not phone:
        logger.warning("[ExotelWA] post-call template skipped: invalid phone=%s lead=%s", lead.phone, lead.id)
        return

    logger.info(
        "[ExotelWA] sending post-call template=%s to lead=%s name=%s phone=%s",
        template_name, lead.id, lead.name, phone,
    )

    try:
        response = await send_template(phone, template_name, language=language)
        await _log_outbound_template(db, phone=phone, template_name=template_name, response=response, error=None)
        logger.info("[ExotelWA] post-call template SENT lead=%s sid=%s", lead.id, _extract_provider_sid(response))
    except HTTPException as exc:
        err = str(exc.detail)
        logger.warning("[ExotelWA] post-call template HTTP-failed lead=%s: %s", lead.id, err[:500])
        try:
            await _log_outbound_template(db, phone=phone, template_name=template_name, response=None, error=err[:500])
        except Exception as log_exc:
            logger.exception("[ExotelWA] additionally failed to log error row: %s", log_exc)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        logger.exception("[ExotelWA] post-call template CRASHED lead=%s", lead.id)
        try:
            await _log_outbound_template(db, phone=phone, template_name=template_name, response=None, error=err[:500])
        except Exception as log_exc:
            logger.exception("[ExotelWA] additionally failed to log error row: %s", log_exc)


async def send_feedback_template(
    lead_id: uuid_lib.UUID,
    db: AsyncSession | None = None,
) -> None:
    """Send the post-visit feedback WhatsApp template to a lead.

    Triggered when a lead is marked "visited" on the dashboard. Sets
    lead.feedback_sent on success so it's only ever sent once. Logs a row to
    whatsapp_messages so the send shows up in the chat UI.
    """
    logger.info("[ExotelWA] feedback template task START for lead=%s", lead_id)
    if db is None:
        try:
            async with AsyncSessionLocal() as session:
                await send_feedback_template(lead_id, session)
        except Exception as exc:
            logger.exception("[ExotelWA] feedback template task CRASHED at session level: %s", exc)
        return

    settings = get_settings()
    template_name = settings.EXOTEL_WA_TEMPLATE_FEEDBACK
    language = settings.EXOTEL_WA_TEMPLATE_FEEDBACK_LANG

    lead = await db.get(Lead, lead_id)
    if not lead:
        logger.warning("[ExotelWA] feedback template skipped: lead %s not found", lead_id)
        return
    if lead.feedback_sent:
        logger.info("[ExotelWA] feedback already sent for lead=%s — skipping", lead_id)
        return

    phone = format_phone_for_whatsapp(lead.phone)
    if not phone:
        logger.warning("[ExotelWA] feedback template skipped: invalid phone=%s lead=%s", lead.phone, lead.id)
        return

    logger.info("[ExotelWA] sending feedback template=%s to lead=%s phone=%s", template_name, lead.id, phone)
    try:
        response = await send_template(phone, template_name, language=language)
        await _log_outbound_template(db, phone=phone, template_name=template_name, response=response, error=None)
        lead.feedback_sent = True
        await db.commit()
        logger.info("[ExotelWA] feedback template SENT lead=%s sid=%s", lead.id, _extract_provider_sid(response))
    except Exception as exc:
        err = f"{type(exc).__name__}: {getattr(exc, 'detail', exc)}"
        logger.warning("[ExotelWA] feedback template failed lead=%s: %s", lead.id, err[:500])
        try:
            await _log_outbound_template(db, phone=phone, template_name=template_name, response=None, error=err[:500])
        except Exception as log_exc:
            logger.exception("[ExotelWA] additionally failed to log error row: %s", log_exc)
