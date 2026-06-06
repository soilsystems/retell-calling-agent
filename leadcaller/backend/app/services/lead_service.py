import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallJob, CallJobStatus, Lead, WebhookEvent
from app.schemas.lead_schema import ZohoLeadWebhook
from app.utils.business_hours import is_business_hours, next_business_slot

logger = logging.getLogger(__name__)


async def upsert_lead(payload: ZohoLeadWebhook, db: AsyncSession) -> Lead:
    stmt = (
        insert(Lead)
        .values(
            zoho_lead_id=payload.zoho_lead_id,
            name=payload.name,
            phone=payload.phone,
            email=payload.email,
            city=payload.city,
            language_preference=payload.language_preference,
            source=payload.source,
            campaign=payload.campaign,
        )
        .on_conflict_do_update(
            index_elements=[Lead.zoho_lead_id],
            set_={
                "name": payload.name,
                "phone": payload.phone,
                "city": payload.city,
                "language_preference": payload.language_preference,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        .returning(Lead)
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def find_active_call_job(lead_id: uuid.UUID, db: AsyncSession) -> CallJob | None:
    stmt: Select[tuple[CallJob]] = select(CallJob).where(
        CallJob.lead_id == lead_id,
        CallJob.status.in_([CallJobStatus.pending, CallJobStatus.in_progress]),
    )
    return (await db.execute(stmt)).scalars().first()


async def schedule_call_for_lead(
    payload: ZohoLeadWebhook,
    webhook_event: WebhookEvent,
    db: AsyncSession,
    now: datetime | None = None,
) -> tuple[str, CallJob | None]:
    now = now or datetime.now(timezone.utc)
    lead = await upsert_lead(payload, db)

    existing_job = await find_active_call_job(lead.id, db)
    if existing_job:
        webhook_event.processed = True
        await db.commit()
        logger.info("Call already scheduled for lead_id=%s", lead.id)
        return "call already scheduled", existing_job

    scheduled_at = now if is_business_hours(now) else next_business_slot(now)
    call_job = CallJob(
        lead_id=lead.id,
        status=CallJobStatus.pending,
        scheduled_at=scheduled_at,
        trigger_reason="new_lead",
    )
    db.add(call_job)
    webhook_event.processed = True
    await db.commit()
    await db.refresh(call_job)
    return "scheduled", call_job
