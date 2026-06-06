from datetime import datetime, timezone
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import CallJob, CrmSyncLog, Lead, WebhookEvent, WebhookSource
from app.services.meta_service import (
    check_page_subscription,
    get_long_lived_page_token,
    process_meta_lead,
    process_simulated_lead,
    subscribe_app_to_page,
)
from app.utils.phone import format_phone_e164

router = APIRouter(prefix="/debug", tags=["debug"])
logger = logging.getLogger(__name__)

# TESTING SEQUENCE FOR META INTEGRATION:
#
# 1. Test server is running:
#    GET /debug/meta/verify-test
#    Expected: JSON with all keys showing true
#
# 2. Test Meta verification manually:
#    curl "https://YOUR_NGROK_URL/webhooks/meta/new-lead
#         ?hub.mode=subscribe
#         &hub.verify_token=soilsystems_meta_verify_2026
#         &hub.challenge=testchallenge123"
#    Expected: testchallenge123
#
# 3. Get page access token:
#    Go to https://developers.facebook.com/tools/explorer
#    Select LeadCaller app
#    Add permissions: pages_manage_metadata, pages_read_engagement, leads_retrieval
#    Generate token -> copy it
#
# 4. Exchange for long-lived token:
#    POST /debug/meta/exchange-token
#    Body: { "short_lived_token": "token_from_step_3" }
#    Copy long_lived_token -> paste into .env as META_PAGE_ACCESS_TOKEN
#
# 5. Simulate a lead end to end:
#    POST /debug/meta/simulate-lead
#    Body: { "name": "Test Lead", "phone": "9876543210",
#            "email": "test@soilsystems.in", "city": "Bengaluru" }
#    Then check:
#    - FastAPI logs for processing steps
#    - Supabase leads table for new record
#    - Supabase call_jobs table for pending job
#    - Zoho CRM Leads for new lead
#    - Retell dashboard for outbound call (if within business hours)
#
# 6. Test with real Meta form:
#    Go to https://developers.facebook.com/tools/lead-ads-testing
#    Select Soil Systems page -> select Test form
#    Submit a test lead
#    Check same things as step 5
#
# COMPLETE TESTING SEQUENCE:
#
# 1. Verify server health:
#    GET /debug/meta/verify-test
#    All keys should show true
#
# 2. Simulate a lead:
#    POST /debug/meta/simulate-lead
#    Body: {"name": "Test", "phone": "9876543210",
#           "email": "test@test.com", "city": "Bengaluru"}
#
# 3. Check lead was created:
#    GET /debug/meta/last-lead
#    Should show the test lead with call_job pending
#
# 4. Check Zoho was updated:
#    GET /debug/zoho/last-lead
#    Should show success=true for create_lead operation
#
# 5. Submit real Meta form:
#    Go to developers.facebook.com/tools/lead-ads-testing
#    Delete existing test lead
#    Create new test lead
#    Click Track status - wait for delivery
#
# 6. Verify real lead:
#    GET /debug/meta/last-lead
#    Should show the real lead from Meta form
#
# 7. Check Zoho CRM:
#    Go to crm.zoho.in -> Leads
#    New lead should appear automatically


@router.get("/meta/verify-test")
async def meta_verify_test() -> dict[str, bool | str]:
    settings = get_settings()
    base_url = settings.BASE_URL.rstrip("/")
    return {
        "status": "ok",
        "message": "If you see this, the server is running",
        "meta_app_id": settings.META_APP_ID,
        "meta_page_id": settings.META_PAGE_ID,
        "verify_token_set": bool(settings.META_VERIFY_TOKEN),
        "app_secret_set": bool(settings.META_APP_SECRET),
        "page_token_set": bool(settings.META_PAGE_ACCESS_TOKEN),
        "webhook_url": f"{base_url}/webhooks/meta/new-lead",
    }


@router.post("/meta/exchange-token")
async def exchange_meta_token(body: dict) -> dict[str, str]:
    short_token = body.get("short_lived_token")
    if not short_token:
        raise HTTPException(status_code=400, detail="short_lived_token required")

    long_token = await get_long_lived_page_token(short_token)
    return {
        "long_lived_token": long_token,
        "note": "Paste this into .env as META_PAGE_ACCESS_TOKEN. Valid for 60 days.",
    }


@router.post("/meta/subscribe-page")
async def subscribe_to_page() -> dict[str, object]:
    """
    Subscribe LeadCaller app to Soil Systems page for leadgen events.
    Call this once to activate webhook delivery to LeadCaller.
    """
    try:
        result = await subscribe_app_to_page()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "success",
        "message": "LeadCaller app subscribed to page leadgen events",
        "result": result,
    }


@router.get("/meta/check-subscription")
async def check_subscription() -> dict[str, object]:
    """
    Check which apps are subscribed to the Soil Systems page.
    LeadCaller app ID 2034194723895151 should appear in the list.
    """
    settings = get_settings()
    try:
        result = await check_page_subscription()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "subscribed_apps": result,
        "leadcaller_app_id": settings.META_APP_ID,
        "note": "Look for app_id 2034194723895151 in the list",
    }


@router.get("/meta/health")
async def meta_health_check() -> dict[str, object]:
    """
    Complete health check for Meta integration.
    Shows status of all components.
    """
    settings = get_settings()
    base_url = settings.BASE_URL.rstrip("/")
    health: dict[str, object] = {
        "config": {
            "app_id_set": bool(settings.META_APP_ID),
            "app_secret_set": bool(settings.META_APP_SECRET),
            "verify_token_set": bool(settings.META_VERIFY_TOKEN),
            "page_id_set": bool(settings.META_PAGE_ID),
            "page_token_set": bool(settings.META_PAGE_ACCESS_TOKEN),
        },
        "webhook_url": f"{base_url}/webhooks/meta/new-lead",
        "instructions": {
            "required_meta_permissions": "pages_manage_metadata, pages_read_engagement, leads_retrieval",
            "step_1": "POST /debug/meta/subscribe-page to subscribe LeadCaller to page",
            "step_2": "GET /debug/meta/check-subscription to verify subscription",
            "step_3": "Submit test lead at developers.facebook.com/tools/lead-ads-testing",
            "step_4": "Check FastAPI logs for [Meta] processing lines",
            "step_5": "Check Supabase leads and call_jobs tables",
        },
    }

    try:
        subscription = await check_page_subscription()
        app_ids = [app.get("id") for app in subscription.get("data", [])]
        health["page_subscription"] = {
            "status": "checked",
            "subscribed_app_ids": app_ids,
            "leadcaller_subscribed": settings.META_APP_ID in app_ids,
        }
    except Exception as exc:
        health["page_subscription"] = {
            "status": "error",
            "error": str(exc),
        }

    return health


@router.post("/meta/simulate-lead")
async def simulate_meta_lead(
    body: dict,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    timestamp = int(datetime.now(timezone.utc).timestamp())
    lead_data = {
        "lead_id": f"SIMULATED_{timestamp}",
        "name": body.get("name", "Test Lead"),
        "phone": format_phone_e164(body.get("phone", "")),
        "email": body.get("email", ""),
        "city": body.get("city", "Bengaluru"),
        "source": "Meta Ads Simulated",
        "campaign": "debug_simulation",
        "language_preference": "english",
        "zoho_lead_id": None,
    }
    background_tasks.add_task(process_simulated_lead, lead_data)
    return {
        "status": "triggered",
        "lead_data": lead_data,
        "note": "Check FastAPI logs and Supabase for results",
    }


@router.post("/meta/replay-webhook")
async def replay_meta_webhook(
    body: dict,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    """
    Replay a Meta webhook payload manually.
    Use this when Meta shows Failure status to test processing.
    Body: paste the exact payload Meta tried to send.
    """
    logger.info("[Debug] Replaying Meta webhook: %s", body)
    background_tasks.add_task(process_meta_lead, body)
    return {"status": "triggered", "payload": body}


@router.get("/meta/last-lead")
async def get_last_meta_leads(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """
    Show last 5 leads that came from Meta Ads.
    Use this to verify end to end flow is working.
    """
    result = await db.execute(
        select(Lead)
        .where(Lead.source.in_(["Meta Ads", "Meta Ads Simulated"]))
        .order_by(Lead.created_at.desc())
        .limit(5)
    )
    leads = result.scalars().all()

    lead_list = []
    for lead in leads:
        job_result = await db.execute(
            select(CallJob)
            .where(CallJob.lead_id == lead.id)
            .order_by(CallJob.created_at.desc())
            .limit(1)
        )
        call_job = job_result.scalar_one_or_none()
        status = getattr(call_job.status, "value", call_job.status) if call_job else None

        lead_list.append(
            {
                "lead_id": str(lead.id),
                "name": lead.name,
                "phone": lead.phone,
                "city": lead.city,
                "zoho_lead_id": lead.zoho_lead_id,
                "source": lead.source,
                "created_at": lead.created_at.isoformat() if lead.created_at else None,
                "call_job": {
                    "id": str(call_job.id) if call_job else None,
                    "status": status,
                    "scheduled_at": (
                        call_job.scheduled_at.isoformat()
                        if call_job and call_job.scheduled_at
                        else None
                    ),
                }
                if call_job
                else None,
            }
        )

    return {
        "total": len(lead_list),
        "leads": lead_list,
        "message": (
            "These are the last 5 leads from Meta Ads"
            if lead_list
            else "No Meta leads found yet"
        ),
    }


@router.get("/meta/webhook-events")
async def get_recent_meta_webhook_events(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """
    Show recent Meta webhook events only.
    Use this while Meta Lead Ads Testing is pending or retrying delivery.
    """
    result = await db.execute(
        select(WebhookEvent)
        .where(WebhookEvent.source == WebhookSource.meta)
        .order_by(WebhookEvent.received_at.desc())
        .limit(10)
    )
    events = result.scalars().all()

    return {
        "total": len(events),
        "events": [
            {
                "id": str(event.id),
                "event_type": event.event_type,
                "processed": event.processed,
                "idempotency_key": event.idempotency_key,
                "received_at": event.received_at.isoformat() if event.received_at else None,
                "payload": event.payload,
            }
            for event in events
        ],
    }


@router.get("/zoho/last-lead")
async def verify_zoho_lead(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    """
    Check last CRM sync log to verify Zoho updates are working.
    """
    result = await db.execute(
        select(CrmSyncLog)
        .order_by(CrmSyncLog.synced_at.desc())
        .limit(5)
    )
    logs = result.scalars().all()

    return {
        "total": len(logs),
        "sync_logs": [
            {
                "operation": log.operation,
                "success": log.success,
                "error_message": log.error_message,
                "synced_at": log.synced_at.isoformat() if log.synced_at else None,
            }
            for log in logs
        ],
    }
