import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import (
    CallAttempt,
    CallJob,
    CrmSyncLog,
    Followup,
    FollowupStatus,
    LanguagePreference,
    Lead,
    WhatsAppLog,
    WhatsAppLogStatus,
    ZohoToken,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(char for char in value if char.isdigit())
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return f"+{digits}"
    if value.startswith("+"):
        return value.strip()
    return value.strip()


def _parse_follow_up_time(value: Any, reference_time: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value

    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    reference = reference_time or _utcnow()
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)

    match = re.search(
        r"(?:after|in)?\s*(?:about\s*)?(\d+|one|two|three|four|five|ten|fifteen|thirty)\s*"
        r"(minute|minutes|min|mins|hour|hours|hr|hrs)",
        text.lower(),
    )
    if not match:
        return None

    numbers = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "ten": 10,
        "fifteen": 15,
        "thirty": 30,
    }
    amount = numbers.get(match.group(1), int(match.group(1)) if match.group(1).isdigit() else None)
    if amount is None:
        return None

    if match.group(2).startswith(("hour", "hr")):
        return reference + timedelta(hours=amount)
    return reference + timedelta(minutes=amount)


def _lead_name(raw: dict[str, Any]) -> str:
    full_name = raw.get("Full_Name") or raw.get("full_name")
    if full_name:
        return str(full_name)
    first = raw.get("First_Name") or ""
    last = raw.get("Last_Name") or ""
    name = f"{first} {last}".strip()
    return name or "Zoho Lead"


def _campaign_name(raw: dict[str, Any]) -> str | None:
    campaign = raw.get("Campaign")
    if isinstance(campaign, dict):
        return campaign.get("name") or campaign.get("id")
    return campaign


async def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.request(method, url, headers=headers, json=json, data=data)
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return response
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Zoho request failed after retries: {last_exc}")


async def get_zoho_access_token(db: AsyncSession) -> str:
    settings = get_settings()
    token = (await db.execute(select(ZohoToken).order_by(ZohoToken.created_at.desc()))).scalars().first()
    if token and token.expires_at > _utcnow() + timedelta(minutes=5):
        return token.access_token

    refresh_token = token.refresh_token if token else settings.ZOHO_REFRESH_TOKEN
    if not refresh_token:
        raise RuntimeError("Zoho refresh token is not configured")

    response = await _request_with_retry(
        "POST",
        f"{settings.ZOHO_ACCOUNTS_DOMAIN}/oauth/v2/token",
        data={
            "refresh_token": refresh_token,
            "client_id": settings.ZOHO_CLIENT_ID,
            "client_secret": settings.ZOHO_CLIENT_SECRET,
            "redirect_uri": settings.ZOHO_REDIRECT_URI,
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    data = response.json()
    expires_at = _utcnow() + timedelta(seconds=int(data.get("expires_in", 3600)))

    if token:
        token.access_token = data["access_token"]
        token.expires_at = expires_at
    else:
        token = ZohoToken(
            access_token=data["access_token"],
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
        db.add(token)
    await db.commit()
    return token.access_token


async def fetch_recent_zoho_leads(db: AsyncSession, limit: int = 100) -> list[dict[str, Any]]:
    settings = get_settings()
    access_token = await get_zoho_access_token(db)
    limit = min(max(limit, 1), 200)
    fields = ",".join(
        [
            "id",
            "Full_Name",
            "First_Name",
            "Last_Name",
            "Phone",
            "Mobile",
            "Email",
            "City",
            "Lead_Source",
            "Campaign",
            "Created_Time",
        ]
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{settings.ZOHO_API_DOMAIN}/crm/v6/Leads",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            params={
                "fields": fields,
                "per_page": limit,
                "sort_by": "Created_Time",
                "sort_order": "desc",
            },
        )
    response.raise_for_status()
    return response.json().get("data") or []


async def sync_recent_zoho_leads(db: AsyncSession, limit: int = 100) -> dict[str, int]:
    raw_leads = await fetch_recent_zoho_leads(db, limit=limit)
    synced = 0
    skipped = 0
    for raw in raw_leads:
        zoho_lead_id = raw.get("id")
        if not zoho_lead_id:
            skipped += 1
            continue

        values = {
            "zoho_lead_id": str(zoho_lead_id),
            "name": _lead_name(raw),
            "phone": _clean_phone(raw.get("Mobile") or raw.get("Phone")),
            "email": raw.get("Email"),
            "city": raw.get("City"),
            "language_preference": LanguagePreference.english,
            "source": raw.get("Lead_Source") or "Zoho CRM",
            "campaign": _campaign_name(raw),
            "updated_at": _utcnow(),
        }
        stmt = (
            insert(Lead)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[Lead.zoho_lead_id],
                set_={
                    "name": values["name"],
                    "phone": values["phone"],
                    "email": values["email"],
                    "city": values["city"],
                    "source": values["source"],
                    "campaign": values["campaign"],
                    "updated_at": values["updated_at"],
                },
            )
        )
        await db.execute(stmt)
        synced += 1

    await db.commit()
    return {"fetched": len(raw_leads), "synced": synced, "skipped": skipped}


async def create_zoho_lead_for_inbound(phone: str, db: AsyncSession) -> str:
    settings = get_settings()
    access_token = await get_zoho_access_token(db)
    body = {
        "data": [
            {
                "Last_Name": "Unknown",
                "Full_Name": "Unknown",
                "Mobile": _clean_phone(phone),
                "Lead_Source": "Inbound Call",
                "Description": "Lead created automatically from inbound Retell call.",
            }
        ]
    }
    response = await _request_with_retry(
        "POST",
        f"{settings.ZOHO_API_DOMAIN}/crm/v6/Leads",
        headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        json=body,
    )
    response.raise_for_status()
    data = response.json()
    details = (data.get("data") or [{}])[0].get("details") or {}
    zoho_lead_id = details.get("id")
    if not zoho_lead_id:
        raise RuntimeError(f"Zoho inbound lead creation returned no id: {data}")
    return str(zoho_lead_id)


async def create_lead_in_zoho(lead_data: dict[str, Any], db: AsyncSession) -> str:
    settings = get_settings()
    access_token = await get_zoho_access_token(db)
    full_name = lead_data.get("name") or "Unknown"
    name_parts = full_name.strip().split(" ", 1)
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else full_name
    description_parts = [
        f"Source: {lead_data.get('source', 'Meta Ads')}",
        f"Campaign: {lead_data.get('campaign', '')}",
    ]
    if lead_data.get("meta_form_id"):
        description_parts.append(f"Meta Form ID: {lead_data['meta_form_id']}")
    if lead_data.get("meta_ad_id"):
        description_parts.append(f"Meta Ad ID: {lead_data['meta_ad_id']}")
    if lead_data.get("meta_ad_name"):
        description_parts.append(f"Meta Ad Name: {lead_data['meta_ad_name']}")
    if lead_data.get("meta_campaign_id"):
        description_parts.append(f"Meta Campaign ID: {lead_data['meta_campaign_id']}")
    if lead_data.get("meta_campaign_name"):
        description_parts.append(f"Meta Campaign Name: {lead_data['meta_campaign_name']}")

    lead_fields = {
        "First_Name": first_name,
        "Last_Name": last_name,
        "Email": lead_data.get("email", ""),
        "City": lead_data.get("city", ""),
        "Lead_Source": "Meta Ads",
        "Campaign_Source__c": lead_data.get("campaign", ""),
        "Lead_Status": "New",
        "Description": " | ".join(description_parts),
    }
    phone = lead_data.get("phone") or ""
    if phone:
        lead_fields["Phone"] = phone
        lead_fields["Mobile"] = phone

    payload = {"data": [lead_fields]}

    try:
        response = await _request_with_retry(
            "POST",
            f"{settings.ZOHO_API_DOMAIN}/crm/v6/Leads",
            headers={
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        logger.info("[Zoho] create_lead response: %s %s", response.status_code, response.text)
        if response.status_code in [200, 201]:
            zoho_lead_id = response.json()["data"][0]["details"]["id"]
            db.add(CrmSyncLog(lead_id=None, operation="create_lead", success=True))
            await db.commit()
            logger.info("[Zoho] Lead created successfully: %s", zoho_lead_id)
            return str(zoho_lead_id)

        db.add(
            CrmSyncLog(
                lead_id=None,
                operation="create_lead",
                success=False,
                error_message=response.text,
            )
        )
        await db.commit()
        logger.error("[Zoho] Lead creation failed: %s", response.text)
        raise RuntimeError(f"Zoho lead creation failed: {response.text}")
    except Exception as exc:
        if not isinstance(exc, RuntimeError):
            db.add(
                CrmSyncLog(
                    lead_id=None,
                    operation="create_lead",
                    success=False,
                    error_message=str(exc),
                )
            )
            await db.commit()
        raise


async def _load_attempt(call_attempt_id: uuid.UUID, db: AsyncSession) -> CallAttempt | None:
    result = await db.execute(
        select(CallAttempt)
        .options(selectinload(CallAttempt.call_job).selectinload(CallJob.lead))
        .where(CallAttempt.id == call_attempt_id)
    )
    return result.scalar_one_or_none()


async def _latest_whatsapp_log(call_attempt_id: uuid.UUID, db: AsyncSession) -> WhatsAppLog | None:
    result = await db.execute(
        select(WhatsAppLog)
        .where(WhatsAppLog.call_attempt_id == call_attempt_id)
        .order_by(WhatsAppLog.sent_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _lead_status(interest_level: str | None) -> str | None:
    return {
        "Hot": "Hot Lead",
        "Warm": "Contacted",
        "Cold": "Cold",
        "Not Interested": "Not Qualified",
    }.get(interest_level or "")


async def sync_to_zoho(call_attempt_id: uuid.UUID, db: AsyncSession | None = None, retry_once: bool = True) -> None:
    if db is None:
        async with AsyncSessionLocal() as session:
            await sync_to_zoho(call_attempt_id, session, retry_once=retry_once)
        return

    settings = get_settings()
    attempt = await _load_attempt(call_attempt_id, db)
    if not attempt:
        return
    lead = attempt.call_job.lead
    structured = attempt.structured_data or {}
    access_token = await get_zoho_access_token(db)

    fields: dict[str, Any] = {
        "AI_Call_Status": attempt.status.value,
        "AI_Call_Summary": attempt.summary,
        "AI_Call_Transcript": (attempt.transcript or "")[:32000],
        "AI_Recording_URL": attempt.recording_url,
        "AI_Lead_Intent": structured.get("interest_level"),
        "AI_Interest_Level": structured.get("interest_level"),
        "AI_Budget": structured.get("budget"),
        "AI_Timeline": structured.get("timeline"),
        "Preferred_Language": structured.get("language"),
        "Follow_up_Required": structured.get("follow_up_required"),
        "Follow_up_DateTime": structured.get("follow_up_time"),
        "AI_Call_Attempt_Count": 1,
        "Last_AI_Call_Time": _utcnow().isoformat(),
        "AI_Callback_Scheduled": bool(structured.get("callback_required")),
        "AI_Callback_Time": structured.get("callback_time"),
        "AI_Call_Direction": getattr(attempt.direction, "value", str(attempt.direction)).title(),
        "AI_Last_Call_Trigger_Reason": attempt.call_job.trigger_reason,
    }
    whatsapp_log = await _latest_whatsapp_log(call_attempt_id, db)
    if whatsapp_log:
        fields["WhatsApp_Sent"] = whatsapp_log.status == WhatsAppLogStatus.sent
        fields["WhatsApp_Template_Sent"] = whatsapp_log.template_name
        fields["WhatsApp_Sent_At"] = whatsapp_log.sent_at.isoformat()

    lead_status = _lead_status(structured.get("interest_level"))
    if lead_status:
        fields["Lead_Status"] = lead_status
    if lead.source == "Inbound Call":
        if lead.name and lead.name.lower() != "unknown":
            fields["Last_Name"] = lead.name
        if lead.email:
            fields["Email"] = lead.email
        if lead.city:
            fields["City"] = lead.city
        caller_requirement = (
            structured.get("caller_requirement")
            or structured.get("caller_details")
            or structured.get("requirement")
            or structured.get("enquiry_details")
        )
        if caller_requirement or attempt.summary:
            fields["Description"] = str(caller_requirement or attempt.summary)[:32000]

    try:
        response = await _request_with_retry(
            "PATCH",
            f"{settings.ZOHO_API_DOMAIN}/crm/v6/Leads/{lead.zoho_lead_id}",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            json={"data": [fields]},
        )
        response.raise_for_status()
        db.add(CrmSyncLog(lead_id=lead.id, operation="update_lead_ai_call", success=True))
        await db.commit()
    except Exception as exc:
        if retry_once:
            await asyncio.sleep(60)
            await sync_to_zoho(call_attempt_id, db, retry_once=False)
            return
        db.add(
            CrmSyncLog(
                lead_id=lead.id,
                operation="update_lead_ai_call",
                success=False,
                error_message=str(exc),
            )
        )
        await db.commit()
        logger.exception("Zoho lead sync failed for call_attempt_id=%s", call_attempt_id)


async def update_zoho_whatsapp_status(
    whatsapp_log_id: uuid.UUID,
    db: AsyncSession | None = None,
    retry_once: bool = True,
) -> None:
    if db is None:
        async with AsyncSessionLocal() as session:
            await update_zoho_whatsapp_status(whatsapp_log_id, session, retry_once=retry_once)
        return

    settings = get_settings()
    result = await db.execute(
        select(WhatsAppLog).options(selectinload(WhatsAppLog.lead)).where(WhatsAppLog.id == whatsapp_log_id)
    )
    whatsapp_log = result.scalar_one_or_none()
    if not whatsapp_log:
        return

    access_token = await get_zoho_access_token(db)
    fields = {
        "WhatsApp_Sent": whatsapp_log.status == WhatsAppLogStatus.sent,
        "WhatsApp_Template_Sent": whatsapp_log.template_name,
        "WhatsApp_Sent_At": whatsapp_log.sent_at.isoformat(),
    }

    try:
        response = await _request_with_retry(
            "PATCH",
            f"{settings.ZOHO_API_DOMAIN}/crm/v6/Leads/{whatsapp_log.lead.zoho_lead_id}",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            json={"data": [fields]},
        )
        response.raise_for_status()
        db.add(CrmSyncLog(lead_id=whatsapp_log.lead_id, operation="update_lead_whatsapp", success=True))
        await db.commit()
    except Exception as exc:
        if retry_once:
            await asyncio.sleep(60)
            await update_zoho_whatsapp_status(whatsapp_log_id, db, retry_once=False)
            return
        db.add(
            CrmSyncLog(
                lead_id=whatsapp_log.lead_id,
                operation="update_lead_whatsapp",
                success=False,
                error_message=str(exc),
            )
        )
        await db.commit()
        logger.exception("Zoho WhatsApp sync failed for whatsapp_log_id=%s", whatsapp_log_id)


async def create_followup_task(call_attempt_id: uuid.UUID, db: AsyncSession | None = None) -> None:
    if db is None:
        async with AsyncSessionLocal() as session:
            await create_followup_task(call_attempt_id, session)
        return

    settings = get_settings()
    attempt = await _load_attempt(call_attempt_id, db)
    if not attempt:
        return
    lead = attempt.call_job.lead
    structured = attempt.structured_data or {}
    follow_up_time = structured.get("follow_up_time")
    if not follow_up_time:
        return
    scheduled_at = _parse_follow_up_time(follow_up_time, attempt.ended_at or attempt.started_at)
    if scheduled_at is None:
        logger.warning("Invalid follow_up_time for call_attempt_id=%s: %s", call_attempt_id, follow_up_time)
        return
    due_date = scheduled_at.isoformat()

    access_token = await get_zoho_access_token(db)
    followup = Followup(
        lead_id=lead.id,
        call_attempt_id=attempt.id,
        scheduled_at=scheduled_at,
        status=FollowupStatus.pending,
    )
    db.add(followup)
    await db.flush()

    body = {
        "data": [
            {
                "Subject": f"Follow up - AI call: {lead.name}",
                "Due_Date": due_date,
                "Status": "Not Started",
                "Priority": "High" if structured.get("interest_level") == "Hot" else "Normal",
                "Description": attempt.summary,
                "$se_module": "Leads",
                "What_Id": {"id": lead.zoho_lead_id},
            }
        ]
    }

    try:
        response = await _request_with_retry(
            "POST",
            f"{settings.ZOHO_API_DOMAIN}/crm/v6/Tasks",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            json=body,
        )
        response.raise_for_status()
        data = response.json()
        details = (data.get("data") or [{}])[0].get("details") or {}
        followup.zoho_task_id = details.get("id")
        followup.status = FollowupStatus.created
    except Exception as exc:
        followup.status = FollowupStatus.failed
        logger.exception("Zoho followup task creation failed for call_attempt_id=%s: %s", call_attempt_id, exc)
    await db.commit()
