"""API Router for manual Exotel WhatsApp automation operations from the LeadCaller dashboard.
"""

import logging
import uuid
from typing import Any, Literal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.lead import Lead
from app.services.whatsapp_service import (
    send_whatsapp_call_completed,
    send_whatsapp_call_missed,
    send_whatsapp_custom,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


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
