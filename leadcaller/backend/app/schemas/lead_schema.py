from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.enums import LanguagePreference


IndianMobile = Annotated[str, StringConstraints(pattern=r"^\+91[6-9]\d{9}$", strict=True)]


class ZohoLeadWebhook(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    zoho_lead_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=2, max_length=100)
    phone: IndianMobile
    email: str | None = Field(default=None, max_length=320)
    city: str | None = Field(default=None, max_length=120)
    language_preference: LanguagePreference = LanguagePreference.english
    source: str | None = Field(default=None, max_length=120)
    campaign: str | None = Field(default=None, max_length=120)
    received_at: datetime | None = None
