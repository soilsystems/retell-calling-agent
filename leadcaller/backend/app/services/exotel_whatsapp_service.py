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
    env: dict[str, Any] = {"from": _from_number(), "to": to, "content": content}
    # Ask Exotel/Meta to POST delivery receipts (DLR) back so we can see whether
    # the message was actually delivered, restricted by Meta, or dropped.
    base = (get_settings().BASE_URL or "").rstrip("/")
    if base:
        env["status_callback"] = f"{base}/webhooks/whatsapp/status"
    return env


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
    header_document: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send a pre-approved WhatsApp template via Exotel.

    Templates bypass the 24-hour conversation window restriction. The template
    must already be approved in your Exotel WhatsApp dashboard.

    header_document: {"link": <pdf url>, "filename": <name>} for templates whose
    approved header is a Document (e.g. woods_and_spices). Required — Meta drops
    the message (EX_TEMPLATE_PARAM_ERROR) if the header component is omitted.
    """
    template: dict[str, Any] = {
        "name": name,
        "language": {"code": language},
    }
    components: list[dict[str, Any]] = []
    if header_document:
        components.append({
            "type": "header",
            "parameters": [{"type": "document", "document": header_document}],
        })
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })
    if components:
        template["components"] = components
    return await _post(
        _envelope(_normalize_to(to), {"type": "template", "template": template})
    )


def _dlr_message_node(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the message dict from an Exotel DLR/status payload (or the payload)."""
    wa = payload.get("whatsapp")
    if isinstance(wa, dict):
        msgs = wa.get("messages")
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
            return msgs[0]
    if isinstance(payload.get("message"), dict):
        return payload["message"]
    return payload


def _classify_dlr_status(msg: dict[str, Any]) -> tuple[str | None, str | None]:
    """Map an Exotel DLR message dict to (coarse_status, detail).

    Exotel DLRs carry the message sid plus exo_detailed_status / exo_status_code
    (e.g. EX_TEMPLATE_PARAM_ERROR, EX_RESTRICTED_BY_META) and a human-readable
    `description` — there is no plain "status" field — so we classify from those.
    Returns status None when we can't tell (leaves the stored status unchanged).
    """
    detail = msg.get("exo_detailed_status") or msg.get("description")
    raw = str(msg.get("status") or msg.get("delivery_status") or msg.get("message_status") or "").lower()
    detailed = str(msg.get("exo_detailed_status") or "").upper()
    if raw in {"sent", "delivered", "read"}:
        return raw, detail
    if raw in {"failed", "undelivered", "rejected", "error"}:
        return "failed", detail
    # Exotel exo_detailed_status taxonomy: EX_MESSAGE_SENT / _DELIVERED / _SEEN are
    # the success ladder (SEEN == read); EX_*_ERROR / restricted / re-engagement are
    # failures. NOTE: success and failure both use 30xxx status codes (30001 sent,
    # 30018 re-engagement, ...), so we must classify by the detailed status string,
    # not the numeric code.
    if "SEEN" in detailed or "READ" in detailed:
        return "read", detail
    if "DELIVERED" in detailed:
        return "delivered", detail
    if "SENT" in detailed:
        return "sent", detail
    if detailed and any(
        tok in detailed for tok in ("ERROR", "FAIL", "RESTRICT", "REENGAGE", "INVALID", "PARAM", "UNDELIVER", "REJECT", "BLOCK")
    ):
        return "failed", detail
    return None, detail  # unknown — leave the stored status unchanged


def extract_dlr(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(provider_message_id, coarse_status, detail) from any Exotel DLR/status payload."""
    msg = _dlr_message_node(payload)
    sid = (
        msg.get("sid") or msg.get("id") or msg.get("message_id")
        or payload.get("sid") or payload.get("message_id")
    )
    status, detail = _classify_dlr_status(msg)
    return (str(sid) if sid else None, status, detail)


async def apply_delivery_status(
    db: AsyncSession,
    provider_message_id: str | None,
    status: str | None,
    detail: str | None,
) -> bool:
    """Update a stored outbound message's delivery status from a DLR.

    Status only moves forward (sent → delivered → read); a late "sent" callback
    never overwrites "read". "failed" always wins. Matched by provider_message_id
    (the Exotel sid we stored when sending). Returns True if a row was updated.
    """
    if not provider_message_id or not status:
        return False
    result = await db.execute(
        select(WhatsAppMessage).where(WhatsAppMessage.provider_message_id == provider_message_id)
    )
    msg = result.scalar_one_or_none()
    if not msg:
        return False
    order = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
    if order.get(status, 0) >= order.get(msg.status or "", 0):
        msg.status = status
        if detail:
            msg.status_detail = detail
        await db.commit()
        return True
    return False


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
        status="failed" if error else "sent",
        status_detail=error,
        raw_payload={"template": template_name, "response": response, "error": error},
    )
    db.add(msg)
    await db.commit()


# Attempts whose post-call template has already been sent/claimed this process
# run. Retell fires call_ended AND call_analyzed (and the call-number path may
# add another), each queuing this task — without this guard the lead gets the
# template 2-3 times, which also burns through Meta's per-user rate limit.
# Membership check + add is atomic in single-threaded asyncio.
_post_call_template_claims: set[str] = set()


async def send_post_call_template(
    call_attempt_id: uuid_lib.UUID,
    db: AsyncSession | None = None,
) -> None:
    """Send the post-call WhatsApp template to a lead after a call ends.

    Triggered from the Retell call_completed webhook. Deduped so each call's
    template goes out exactly once. Always writes a row to whatsapp_messages so
    the chat UI reflects what happened.
    """
    key = str(call_attempt_id)
    if key in _post_call_template_claims:
        logger.info("[ExotelWA] post-call template already sent/claimed for attempt=%s — skipping", call_attempt_id)
        return
    if len(_post_call_template_claims) > 5000:
        _post_call_template_claims.clear()
    _post_call_template_claims.add(key)

    logger.info("[ExotelWA] post-call template task START for attempt=%s", call_attempt_id)
    if db is None:
        try:
            async with AsyncSessionLocal() as session:
                await _send_post_call_template_inner(call_attempt_id, session, key)
        except Exception as exc:
            _post_call_template_claims.discard(key)
            logger.exception("[ExotelWA] post-call template task CRASHED at session level: %s", exc)
        return
    await _send_post_call_template_inner(call_attempt_id, db, key)


async def _send_post_call_template_inner(
    call_attempt_id: uuid_lib.UUID,
    db: AsyncSession,
    claim_key: str,
) -> None:

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

    # Attach the required Document header (brochure PDF) — without it Meta drops
    # the message with EX_TEMPLATE_PARAM_ERROR.
    header_document = None
    doc_url = getattr(settings, "EXOTEL_WA_TEMPLATE_POST_CALL_DOC_URL", None)
    if doc_url:
        header_document = {
            "link": doc_url,
            "filename": getattr(settings, "EXOTEL_WA_TEMPLATE_POST_CALL_DOC_NAME", "brochure.pdf"),
        }

    logger.info(
        "[ExotelWA] sending post-call template=%s to lead=%s name=%s phone=%s (doc=%s)",
        template_name, lead.id, lead.name, phone, bool(header_document),
    )

    try:
        response = await send_template(phone, template_name, language=language, header_document=header_document)
        await _log_outbound_template(db, phone=phone, template_name=template_name, response=response, error=None)
        logger.info("[ExotelWA] post-call template SENT lead=%s sid=%s", lead.id, _extract_provider_sid(response))
    except HTTPException as exc:
        _post_call_template_claims.discard(claim_key)  # allow retry on the next event
        err = str(exc.detail)
        logger.warning("[ExotelWA] post-call template HTTP-failed lead=%s: %s", lead.id, err[:500])
        try:
            await _log_outbound_template(db, phone=phone, template_name=template_name, response=None, error=err[:500])
        except Exception as log_exc:
            logger.exception("[ExotelWA] additionally failed to log error row: %s", log_exc)
    except Exception as exc:
        _post_call_template_claims.discard(claim_key)  # allow retry on the next event
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
    """Send a post-visit feedback WhatsApp to a lead as a PLAIN TEXT message
    (not a template), triggered when the lead is marked "visited".

    NOTE: WhatsApp only delivers free-text inside the lead's 24-hour
    conversation window (i.e. they messaged us in the last 24h). Outside it,
    Meta requires a template. Sets lead.feedback_sent on success so it's only
    sent once; logs to whatsapp_messages so it shows in the chat UI.
    """
    logger.info("[ExotelWA] feedback message task START for lead=%s", lead_id)
    if db is None:
        try:
            async with AsyncSessionLocal() as session:
                await send_feedback_template(lead_id, session)
        except Exception as exc:
            logger.exception("[ExotelWA] feedback message task CRASHED at session level: %s", exc)
        return

    settings = get_settings()
    message = settings.WA_FEEDBACK_MESSAGE

    lead = await db.get(Lead, lead_id)
    if not lead:
        logger.warning("[ExotelWA] feedback message skipped: lead %s not found", lead_id)
        return
    if lead.feedback_sent:
        logger.info("[ExotelWA] feedback already sent for lead=%s — skipping", lead_id)
        return

    phone = format_phone_for_whatsapp(lead.phone)
    if not phone:
        logger.warning("[ExotelWA] feedback message skipped: invalid phone=%s lead=%s", lead.phone, lead.id)
        return

    logger.info("[ExotelWA] sending feedback TEXT to lead=%s phone=%s", lead.id, phone)
    try:
        response = await send_text(phone, message)
        # Log the outbound text into the chat thread.
        msg = WhatsAppMessage(
            phone=phone,
            direction=WhatsAppMessageDirection.outbound,
            message_type=WhatsAppMessageType.text,
            body=message,
            provider_message_id=_extract_provider_sid(response),
            status="sent",
            raw_payload={"feedback": True, "response": response},
        )
        db.add(msg)
        lead.feedback_sent = True
        await db.commit()
        logger.info("[ExotelWA] feedback TEXT SENT lead=%s sid=%s", lead.id, _extract_provider_sid(response))
    except Exception as exc:
        err = f"{type(exc).__name__}: {getattr(exc, 'detail', exc)}"
        logger.warning(
            "[ExotelWA] feedback TEXT failed lead=%s: %s (note: free-text needs an open 24h window)",
            lead.id, err[:500],
        )
