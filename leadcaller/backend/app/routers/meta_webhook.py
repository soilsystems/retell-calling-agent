import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.services.meta_service import process_meta_lead
from app.utils.security import verify_meta_signature

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/new-lead")
async def meta_webhook_verify(request: Request) -> PlainTextResponse:
    settings = get_settings()
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.META_VERIFY_TOKEN and challenge is not None:
        logger.info("[Meta] Webhook verified successfully")
        return PlainTextResponse(content=challenge)

    logger.warning("[Meta] Webhook verification failed - token mismatch")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/new-lead")
async def meta_webhook_lead(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_meta_signature(payload, signature):
        logger.warning("[Meta] Invalid signature - ignoring")
        return {"status": "ok"}

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning("[Meta] Invalid webhook JSON: %s", exc)
        return {"status": "ok"}

    logger.info("[Meta] Webhook received: %s", json.dumps(data))
    background_tasks.add_task(process_meta_lead, data)
    return {"status": "ok"}
