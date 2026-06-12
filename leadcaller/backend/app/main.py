import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers.admin import router as admin_router
from app.routers.debug import router as debug_router
from app.routers.meta_webhook import router as meta_router
from app.routers.webhooks import router as webhooks_router
from app.routers.whatsapp import router as whatsapp_router
from app.services.retell_service import run_scheduled_calls


settings = get_settings()
scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[Startup] Meta webhook URL: %s/webhooks/meta/new-lead", settings.BASE_URL.rstrip("/"))
    logger.info("[Startup] Make sure this matches the webhook URL in Meta Developer dashboard")
    if settings.SCHEDULER_ENABLED:
        scheduler.add_job(
            run_scheduled_calls,
            trigger="interval",
            minutes=1,
            id="scheduled_calls",
            replace_existing=True,
        )
        scheduler.start()
    try:
        from app.services.meta_service import check_page_subscription

        subscription = await check_page_subscription()
        app_ids = [app.get("id") for app in subscription.get("data", [])]
        if settings.META_APP_ID not in app_ids:
            logger.warning(
                "[Meta] LeadCaller app NOT subscribed to page. "
                "Call POST /debug/meta/subscribe-page to fix this."
            )
        else:
            logger.info("[Meta] LeadCaller app is subscribed to page")
    except Exception as exc:
        logger.warning("[Meta] Could not check page subscription: %s", exc)
    yield
    if scheduler.running:
        scheduler.shutdown()


app = FastAPI(
    title="LeadCaller",
    version="1.0.0",
    description="Production-ready AI lead qualification system.",
    lifespan=lifespan,
)

_extra_origins = [o.strip() for o in settings.CORS_ALLOW_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        *_extra_origins,
    ],
    allow_origin_regex=settings.CORS_ALLOW_ORIGIN_REGEX or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info("[Request] %s %s", request.method, request.url.path)
    if request.method == "POST":
        headers = dict(request.headers)
        for key in ("authorization", "cookie", "x-hub-signature-256"):
            if key in headers:
                headers[key] = "***"
        logger.info("[Request] Headers: %s", headers)
    response = await call_next(request)
    logger.info("[Response] %s -> %s", request.url.path, response.status_code)
    return response


app.include_router(admin_router)
app.include_router(webhooks_router)
app.include_router(whatsapp_router)
app.include_router(meta_router, prefix="/webhooks/meta", tags=["Meta Webhook"])

if settings.ENVIRONMENT != "prod":
    app.include_router(debug_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.ENVIRONMENT}
