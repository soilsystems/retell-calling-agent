from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    DATABASE_URL: PostgresDsn | str = Field(
        default="postgresql+asyncpg://postgres:postgres@db:5432/leadcaller"
    )

    ZOHO_CLIENT_ID: str
    ZOHO_CLIENT_SECRET: str
    ZOHO_REDIRECT_URI: str
    ZOHO_WEBHOOK_SECRET: str
    ZOHO_REFRESH_TOKEN: str | None = None
    ZOHO_API_DOMAIN: str = "https://www.zohoapis.com"
    ZOHO_ACCOUNTS_DOMAIN: str = "https://accounts.zoho.com"

    META_APP_ID: str
    META_APP_SECRET: str
    META_VERIFY_TOKEN: str
    META_PAGE_ID: str = ""
    META_PAGE_ACCESS_TOKEN: str = ""

    # Meta WhatsApp Cloud API (direct)
    META_WA_PHONE_NUMBER_ID: str | None = None
    META_WA_ACCESS_TOKEN: str | None = None

    # Master switch for WhatsApp sending. Set to False until Meta business
    # verification for SOIL_SYSTEMS is complete — template messages silently
    # fail otherwise and clog retry queues.
    WHATSAPP_ENABLED: bool = False

    RETELL_API_KEY: str
    RETELL_AGENT_ID: str
    RETELL_INBOUND_AGENT_ID: str | None = None
    RETELL_AGENT_VERSION: int | None = None
    RETELL_FROM_NUMBER: str
    RETELL_WEBHOOK_SECRET: str
    RETELL_IMPORT_PHONE_NUMBER_ENDPOINT: str = "https://api.retellai.com/v2/import-phone-number"

    WATI_API_ENDPOINT: str | None = None
    WATI_API_TOKEN: str | None = None

    EXOTEL_ACCOUNT_SID: str | None = None
    EXOTEL_API_KEY: str | None = None
    EXOTEL_API_TOKEN: str | None = None
    EXOTEL_WHATSAPP_NUMBER: str | None = None
    EXOTEL_WHATSAPP_FROM_NUMBER: str | None = None
    EXOTEL_SUBDOMAIN: str | None = None
    EXOTEL_TRUNK_SID: str | None = None
    EXOTEL_PHONE_NUMBER: str | None = None
    EXOTEL_TERMINATION_URI: str | None = None
    EXOTEL_SIP_AUTH_USERNAME: str | None = None
    EXOTEL_SIP_AUTH_PASSWORD: str | None = None
    EXOTEL_TRANSPORT: Literal["TLS", "TCP", "UDP"] = "TLS"
    EXOTEL_CALLER_ID: str | None = None
    EXOTEL_EXOML_URL: str | None = None
    EXOTEL_STATUS_CALLBACK: str | None = None
    # Timezone Exotel reports Call StartTime/DateCreated in (account-local, no offset).
    EXOTEL_TIMEZONE: str = "Asia/Kolkata"
    EXOTEL_CALL_TYPE: Literal["trans", "promo"] = "trans"

    # Exotel WhatsApp Business API
    EXOTEL_WA_API_KEY: str | None = None
    EXOTEL_WA_API_TOKEN: str | None = None
    EXOTEL_WA_ACCOUNT_SID: str | None = None
    EXOTEL_WA_SUBDOMAIN: str = "api.in.exotel.com"
    EXOTEL_WA_PHONE_NUMBER: str | None = None
    EXOTEL_WA_TEMPLATE_SOIL_SYSTEMS: str = "soil_systems"
    EXOTEL_WA_TEMPLATE_COMPLETED: str = "call_followup"
    EXOTEL_WA_TEMPLATE_MISSED: str = "call_missed"
    # Template sent automatically to every lead after a call ends.
    EXOTEL_WA_TEMPLATE_POST_CALL: str = "woods_and_spices"
    EXOTEL_WA_TEMPLATE_POST_CALL_LANG: str = "en"
    # Template sent when a lead is manually marked as "visited" on the dashboard.
    EXOTEL_WA_TEMPLATE_FEEDBACK: str = "visit_feedback"
    EXOTEL_WA_TEMPLATE_FEEDBACK_LANG: str = "en"
    BOOKING_LINK: str = "https://soilsystems.in/book"

    BASE_URL: str
    ENVIRONMENT: Literal["dev", "staging", "prod"] = "dev"
    LOG_LEVEL: str = "INFO"
    SCHEDULER_ENABLED: bool = True

    # Comma-separated list of additional allowed CORS origins (e.g. Vercel deploys).
    # Local dev origins are always allowed.
    CORS_ALLOW_ORIGINS: str = ""
    # Regex matching allowed origins. Default permits any *.vercel.app preview/prod URL.
    CORS_ALLOW_ORIGIN_REGEX: str = r"https://.*\.vercel\.app"


@lru_cache
def get_settings() -> Settings:
    return Settings()
