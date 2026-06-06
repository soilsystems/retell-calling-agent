import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import CallJob, CallJobStatus, Lead, WebhookEvent, WebhookSource
from app.schemas.lead_schema import ZohoLeadWebhook
from app.services.lead_service import upsert_lead
from app.services.zoho_service import create_lead_in_zoho
from app.utils.business_hours import is_business_hours, next_business_slot
from app.utils.phone import format_phone_e164

# HOW TO GET META_PAGE_ACCESS_TOKEN:
# 1. Go to https://developers.facebook.com/tools/explorer
# 2. Top right - select app: LeadCaller
# 3. Click "Generate Access Token"
# 4. Check these permissions:
#    - pages_manage_metadata
#    - pages_read_engagement
#    - leads_retrieval
#    - pages_manage_ads (optional but recommended)
# 5. Click Generate Token -> authorize with Facebook
# 6. Copy the token -> paste into .env as META_PAGE_ACCESS_TOKEN
# 7. Note: this token expires in 1 hour
#    For production, generate a long-lived page token:
#    GET https://graph.facebook.com/v19.0/oauth/access_token
#    ?grant_type=fb_exchange_token
#    &client_id={META_APP_ID}
#    &client_secret={META_APP_SECRET}
#    &fb_exchange_token={SHORT_LIVED_TOKEN}

logger = logging.getLogger(__name__)


META_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "full_name",
        "fullname",
        "name",
        "your_name",
        "contact_name",
        "customer_name",
    ),
    "first_name": ("first_name", "first name"),
    "last_name": ("last_name", "last name"),
    "phone": (
        "phone_number",
        "phone",
        "mobile",
        "mobile_number",
        "contact_number",
        "whatsapp_number",
    ),
    "email": ("email", "email_address"),
    "city": (
        "city",
        "location",
        "state",
        "current_city",
        "your_city",
        "where_are_you_from",
    ),
    "language_preference": (
        "language_preference",
        "preferred_language",
        "language",
    ),
}


def _normalize_meta_field_name(name: str) -> str:
    return (
        name.strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("__", "_")
    )


def _extract_first_value(fields: dict[str, str], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = fields.get(_normalize_meta_field_name(alias))
        if value:
            return value.strip()
    return ""


def _normalize_meta_lead_fields(data: dict[str, Any], leadgen_id: str) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for item in data.get("field_data", []):
        name = _normalize_meta_field_name(str(item.get("name", "")))
        values = item.get("values", [])
        fields[name] = str(values[0]).strip() if values else ""

    name = _extract_first_value(fields, META_FIELD_ALIASES["name"])
    if not name:
        name = (
            f"{_extract_first_value(fields, META_FIELD_ALIASES['first_name'])} "
            f"{_extract_first_value(fields, META_FIELD_ALIASES['last_name'])}"
        ).strip()

    phone = _extract_first_value(fields, META_FIELD_ALIASES["phone"])
    email = _extract_first_value(fields, META_FIELD_ALIASES["email"])
    city = _extract_first_value(fields, META_FIELD_ALIASES["city"])
    language = _extract_first_value(fields, META_FIELD_ALIASES["language_preference"]) or "english"
    campaign = (
        data.get("campaign_name")
        or data.get("campaign_id")
        or data.get("ad_name")
        or data.get("ad_id")
        or ""
    )

    return {
        "lead_id": leadgen_id,
        "name": name or "Unknown",
        "phone": format_phone_e164(phone),
        "email": email,
        "city": city,
        "source": "Meta Ads",
        "campaign": str(campaign),
        "language_preference": language.strip().lower() or "english",
        "meta_form_id": data.get("form_id", ""),
        "meta_ad_id": data.get("ad_id", ""),
        "meta_ad_name": data.get("ad_name", ""),
        "meta_campaign_id": data.get("campaign_id", ""),
        "meta_campaign_name": data.get("campaign_name", ""),
        "meta_raw_fields": fields,
    }


async def get_long_lived_page_token(short_lived_token: str) -> str:
    """
    Exchange a short-lived token (1 hour) for a long-lived token (60 days).
    Call this once after getting token from Graph API Explorer.
    Store the result in META_PAGE_ACCESS_TOKEN in .env.
    """
    settings = get_settings()
    url = "https://graph.facebook.com/v19.0/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": settings.META_APP_ID,
        "client_secret": settings.META_APP_SECRET,
        "fb_exchange_token": short_lived_token,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, params=params)

    if response.status_code == 200:
        data = response.json()
        return data.get("access_token", "")

    raise RuntimeError(f"Token exchange failed: {response.text}")


async def _list_accessible_pages() -> list[dict[str, Any]]:
    settings = get_settings()
    url = "https://graph.facebook.com/v19.0/me/accounts"
    headers = {"Authorization": f"Bearer {settings.META_PAGE_ACCESS_TOKEN}"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, headers=headers)

    if response.status_code == 200:
        return response.json().get("data", [])

    raise RuntimeError(f"List pages failed: {response.text}")


async def _resolve_page_credentials() -> tuple[str, str]:
    settings = get_settings()
    pages = await _list_accessible_pages()
    configured_page_id = settings.META_PAGE_ID.strip()

    if configured_page_id:
        for page in pages:
            if str(page.get("id")) == configured_page_id:
                page_token = page.get("access_token")
                if not page_token:
                    raise RuntimeError(f"Page {configured_page_id} did not include an access token")
                return configured_page_id, str(page_token)
        raise RuntimeError(f"META_PAGE_ID {configured_page_id} not found in accessible pages")

    if len(pages) == 1:
        page = pages[0]
        page_id = page.get("id")
        page_token = page.get("access_token")
        if page_id and page_token:
            return str(page_id), str(page_token)

    raise RuntimeError("META_PAGE_ID is required when multiple or no pages are accessible")


async def subscribe_app_to_page() -> dict[str, Any]:
    """
    Subscribe the LeadCaller app to the Soil Systems page for leadgen webhook events.
    Call this once after setting up META_PAGE_ACCESS_TOKEN.
    """
    page_id, page_access_token = await _resolve_page_credentials()
    url = f"https://graph.facebook.com/v19.0/{page_id}/subscribed_apps"
    params = {
        "subscribed_fields": "leadgen",
    }
    headers = {"Authorization": f"Bearer {page_access_token}"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, params=params, headers=headers)

    logger.info("[Meta] Page subscription response: %s %s", response.status_code, response.text)

    if response.status_code == 200:
        return {"status": "success", "response": response.json()}

    raise RuntimeError(f"Page subscription failed: {response.text}")


async def check_page_subscription() -> dict[str, Any]:
    """
    Check which apps are currently subscribed to the page.
    Use this to verify LeadCaller is subscribed.
    """
    page_id, page_access_token = await _resolve_page_credentials()
    url = f"https://graph.facebook.com/v19.0/{page_id}/subscribed_apps"
    headers = {"Authorization": f"Bearer {page_access_token}"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, headers=headers)

    if response.status_code == 200:
        return response.json()

    raise RuntimeError(f"Check subscription failed: {response.text}")


async def fetch_meta_lead_data(leadgen_id: str) -> dict[str, Any]:
    _, page_access_token = await _resolve_page_credentials()
    url = f"https://graph.facebook.com/v19.0/{leadgen_id}"
    headers = {"Authorization": f"Bearer {page_access_token}"}
    params = {
        "fields": (
            "id,created_time,field_data,form_id,ad_id,ad_name,"
            "campaign_id,campaign_name"
        )
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers, params=params)

    logger.info("[Meta] Graph API response: %s %s", response.status_code, response.text)
    if response.status_code != 200:
        raise RuntimeError(f"Meta Graph API error: {response.text}")

    data = response.json()
    lead_data = _normalize_meta_lead_fields(data, leadgen_id)
    logger.info("[Meta] Normalized field mapping: %s", json.dumps(lead_data, default=str))
    return lead_data


async def check_webhook_event_exists(db: AsyncSession, leadgen_id: str, source: str = "meta") -> bool:
    result = await db.execute(
        select(WebhookEvent).where(
            WebhookEvent.source == WebhookSource(source),
            WebhookEvent.idempotency_key == leadgen_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def record_webhook_event(
    db: AsyncSession,
    leadgen_id: str,
    source: str,
    event_type: str,
    payload: dict[str, Any],
) -> WebhookEvent:
    event = WebhookEvent(
        source=WebhookSource(source),
        event_type=event_type,
        payload=payload,
        processed=False,
        idempotency_key=leadgen_id,
        received_at=datetime.now(timezone.utc),
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def mark_webhook_processed(db: AsyncSession, leadgen_id: str) -> None:
    result = await db.execute(
        select(WebhookEvent).where(
            WebhookEvent.source == WebhookSource.meta,
            WebhookEvent.idempotency_key == leadgen_id,
        )
    )
    event = result.scalar_one_or_none()
    if event:
        event.processed = True
        await db.commit()


async def trigger_new_lead_call(call_job_id, db: AsyncSession) -> None:
    from app.services.retell_service import trigger_retell_call

    await trigger_retell_call(call_job_id, db)


def _extract_leadgen_id(data: dict[str, Any]) -> str | None:
    entry = (data.get("entry") or [{}])[0]
    changes = entry.get("changes") or [{}]
    value = (changes[0] if changes else {}).get("value") or {}
    return value.get("leadgen_id")


def _extract_leadgen_value(data: dict[str, Any]) -> dict[str, Any]:
    entry = (data.get("entry") or [{}])[0]
    changes = entry.get("changes") or [{}]
    return (changes[0] if changes else {}).get("value") or {}


def _meta_fallback_lead_id(leadgen_id: str) -> str:
    return f"meta:{leadgen_id}"


async def _upsert_meta_lead_without_phone(lead_data: dict[str, Any], db: AsyncSession) -> Lead:
    zoho_or_meta_id = lead_data.get("zoho_lead_id") or _meta_fallback_lead_id(str(lead_data["lead_id"]))
    result = await db.execute(select(Lead).where(Lead.zoho_lead_id == zoho_or_meta_id).limit(1))
    lead = result.scalar_one_or_none()
    if lead is None:
        lead = Lead(
            zoho_lead_id=zoho_or_meta_id,
            name=lead_data.get("name") or "Unknown",
            phone="",
            email=lead_data.get("email") or None,
            city=lead_data.get("city") or None,
            source=lead_data.get("source") or "Meta Ads",
            campaign=lead_data.get("campaign") or None,
        )
        db.add(lead)
    else:
        lead.name = lead_data.get("name") or lead.name
        lead.email = lead_data.get("email") or lead.email
        lead.city = lead_data.get("city") or lead.city
        lead.source = lead_data.get("source") or lead.source
        lead.campaign = lead_data.get("campaign") or lead.campaign
        lead.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(lead)
    return lead


def _build_lead_payload(lead_data: dict[str, Any]) -> ZohoLeadWebhook:
    leadgen_id = str(lead_data["lead_id"])
    return ZohoLeadWebhook.model_validate(
        {
            "zoho_lead_id": lead_data.get("zoho_lead_id") or _meta_fallback_lead_id(leadgen_id),
            "name": lead_data.get("name") or "Unknown",
            "phone": lead_data["phone"],
            "email": lead_data.get("email") or None,
            "city": lead_data.get("city") or None,
            "language_preference": lead_data.get("language_preference") or "english",
            "source": lead_data.get("source") or "Meta Ads",
            "campaign": lead_data.get("campaign") or None,
            "received_at": datetime.now(timezone.utc),
        }
    )


async def process_meta_lead(data: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        try:
            await _process_meta_lead(data, db)
        except Exception as exc:
            logger.error("[Meta] Failed: %s", exc, exc_info=True)


async def _process_meta_lead(data: dict[str, Any], db: AsyncSession) -> None:

    try:
        logger.info("[Meta] Raw webhook payload: %s", json.dumps(data))
        value = _extract_leadgen_value(data)
        leadgen_id = value.get("leadgen_id")
        page_id = value.get("page_id")
        form_id = value.get("form_id")
        logger.info("[Meta] leadgen_id=%s page_id=%s form_id=%s", leadgen_id, page_id, form_id)

        if not leadgen_id:
            logger.warning("[Meta] No leadgen_id found in payload")
            return

        logger.info("[Meta] ====== START processing leadgen_id=%s ======", leadgen_id)
        logger.info("[Meta] Step 1: Idempotency check for leadgen_id=%s", leadgen_id)
        if await check_webhook_event_exists(db, leadgen_id, "meta"):
            logger.info("[Meta] Already processed leadgen_id=%s", leadgen_id)
            return

        await record_webhook_event(db, leadgen_id, "meta", "leadgen", data)

        logger.info("[Meta] Step 2: Fetching lead from Graph API")
        lead_data = await fetch_meta_lead_data(leadgen_id)
        lead_data["lead_id"] = lead_data.get("lead_id") or leadgen_id
        logger.info(
            "[Meta] Step 3: Lead data = name=%s phone=%s city=%s",
            lead_data.get("name"),
            lead_data.get("phone"),
            lead_data.get("city"),
        )

        logger.info("[Meta] Step 4: Creating lead in Zoho CRM")
        try:
            zoho_lead_id = await create_lead_in_zoho(lead_data, db)
            lead_data["zoho_lead_id"] = zoho_lead_id
            logger.info("[Meta] Step 4: Zoho lead created successfully zoho_lead_id=%s", zoho_lead_id)
        except Exception as exc:
            logger.error("[Meta] Step 4: Zoho creation failed - continuing: %s", exc, exc_info=True)
            lead_data["zoho_lead_id"] = None

        logger.info("[Meta] Step 5: Upserting lead in Supabase")
        if not lead_data.get("phone"):
            logger.warning("[Meta] Lead %s has no phone - skipping call job", leadgen_id)
            lead = await _upsert_meta_lead_without_phone(lead_data, db)
            logger.info("[Meta] Step 5: Lead upserted lead_id=%s", lead.id)
            await mark_webhook_processed(db, leadgen_id)
            logger.info("[Meta] ====== COMPLETE leadgen_id=%s ======", leadgen_id)
            return

        try:
            lead_payload = _build_lead_payload(lead_data)
        except ValidationError as exc:
            logger.warning("[Meta] Lead %s failed validation: %s", leadgen_id, exc)
            await mark_webhook_processed(db, leadgen_id)
            return

        lead = await upsert_lead(lead_payload, db)
        logger.info("[Meta] Step 5: Lead upserted lead_id=%s", lead.id)

        now_utc = datetime.now(timezone.utc)
        scheduled_at = now_utc if is_business_hours(now_utc) else next_business_slot(now_utc)
        logger.info("[Meta] Step 6: Business hours check scheduled_at=%s", scheduled_at)
        call_job = CallJob(
            lead_id=lead.id,
            status=CallJobStatus.pending,
            scheduled_at=scheduled_at,
            retry_count=0,
            max_retries=3,
            trigger_reason="new_lead_meta",
        )
        db.add(call_job)
        await db.commit()
        await db.refresh(call_job)
        logger.info("[Meta] Step 7: Call job created call_job_id=%s", call_job.id)
        if scheduled_at <= now_utc:
            await trigger_new_lead_call(call_job.id, db)

        await mark_webhook_processed(db, leadgen_id)
        logger.info("[Meta] ====== COMPLETE leadgen_id=%s ======", leadgen_id)
    except Exception as exc:
        logger.error("[Meta] process_meta_lead failed: %s", exc, exc_info=True)


async def process_simulated_lead(lead_data: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        try:
            logger.info("[Meta Simulate] Processing: %s", lead_data)

            if not lead_data.get("phone"):
                logger.warning("[Meta Simulate] Missing phone - skipping")
                return

            zoho_lead_id = lead_data.get("zoho_lead_id")
            try:
                zoho_lead_id = await create_lead_in_zoho(lead_data, db)
                lead_data["zoho_lead_id"] = zoho_lead_id
                logger.info("[Meta Simulate] Zoho lead created: %s", zoho_lead_id)
            except Exception as exc:
                logger.warning("[Meta Simulate] Zoho creation failed: %s", exc)
                lead_data["zoho_lead_id"] = zoho_lead_id or lead_data["lead_id"]

            lead_payload = ZohoLeadWebhook.model_validate(
                {
                    "zoho_lead_id": lead_data["zoho_lead_id"],
                    "name": lead_data.get("name") or "Test Lead",
                    "phone": lead_data["phone"],
                    "email": lead_data.get("email") or None,
                    "city": lead_data.get("city") or None,
                    "language_preference": lead_data.get("language_preference") or "english",
                    "source": lead_data.get("source") or "Meta Ads Simulated",
                    "campaign": lead_data.get("campaign") or None,
                    "received_at": datetime.now(timezone.utc),
                }
            )
            lead = await upsert_lead(lead_payload, db)
            logger.info("[Meta Simulate] Lead in Supabase: %s", lead.id)

            now_utc = datetime.now(timezone.utc)
            call_job = CallJob(
                lead_id=lead.id,
                status=CallJobStatus.pending,
                scheduled_at=now_utc,
                retry_count=0,
                max_retries=3,
                trigger_reason="new_lead_simulated",
            )
            db.add(call_job)
            await db.commit()
            await db.refresh(call_job)
            logger.info("[Meta Simulate] Call job created: %s", call_job.id)
            await trigger_new_lead_call(call_job.id, db)
        except Exception as exc:
            logger.error("[Meta Simulate] Failed: %s", exc, exc_info=True)
