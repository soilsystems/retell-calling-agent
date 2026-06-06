import logging
from datetime import datetime, timezone

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CallJob, CallJobStatus, Lead, WebhookEvent
from app.schemas.lead_schema import ZohoLeadWebhook
logger = logging.getLogger(__name__)


async def upsert_lead(payload: ZohoLeadWebhook, db: AsyncSession) -> Lead:
    existing_by_phone = (
        await db.execute(select(Lead).where(Lead.phone == payload.phone).limit(1))
    ).scalar_one_or_none()
    if existing_by_phone:
        if (
            not payload.zoho_lead_id.startswith("meta:")
            or existing_by_phone.zoho_lead_id.startswith("meta:")
        ):
            existing_by_phone.zoho_lead_id = payload.zoho_lead_id
        existing_by_phone.name = payload.name
        existing_by_phone.email = payload.email
        existing_by_phone.city = payload.city
        existing_by_phone.language_preference = payload.language_preference
        existing_by_phone.source = payload.source
        existing_by_phone.campaign = payload.campaign
        existing_by_phone.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return existing_by_phone

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
                "email": payload.email,
                "city": payload.city,
                "language_preference": payload.language_preference,
                "source": payload.source,
                "campaign": payload.campaign,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        .returning(Lead)
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def find_active_call_job(lead_id, db: AsyncSession) -> CallJob | None:
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

    call_job = CallJob(
        lead_id=lead.id,
        status=CallJobStatus.pending,
        scheduled_at=now,
        trigger_reason="new_lead",
    )
    db.add(call_job)
    webhook_event.processed = True
    await db.commit()
    await db.refresh(call_job)
    return "scheduled", call_job
