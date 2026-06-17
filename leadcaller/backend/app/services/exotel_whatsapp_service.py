"""Exotel WhatsApp send-message API.

POST https://<api_key>:<api_token>@<subdomain>/v2/accounts/<account_sid>/messages

Supports text, image, document, video, audio, location.
"""

import logging
from typing import Any

import httpx
from fastapi import HTTPException

from app.config import get_settings
from app.services.whatsapp_service import format_phone_for_whatsapp

logger = logging.getLogger(__name__)


def _exotel_url() -> str:
    settings = get_settings()
    api_key = settings.EXOTEL_API_KEY
    api_token = settings.EXOTEL_API_TOKEN
    account_sid = settings.EXOTEL_ACCOUNT_SID
    subdomain = (settings.EXOTEL_SUBDOMAIN or "api.exotel.com").strip()
    if not (api_key and api_token and account_sid):
        raise HTTPException(
            status_code=500,
            detail="Exotel WhatsApp send requires EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_ACCOUNT_SID",
        )
    return f"https://{api_key}:{api_token}@{subdomain}/v2/accounts/{account_sid}/messages"


def _from_number() -> str:
    settings = get_settings()
    src = settings.EXOTEL_WHATSAPP_FROM_NUMBER or settings.EXOTEL_WHATSAPP_NUMBER
    if not src:
        raise HTTPException(status_code=500, detail="EXOTEL_WHATSAPP_FROM_NUMBER not configured")
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
