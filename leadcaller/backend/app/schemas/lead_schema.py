from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

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

    @model_validator(mode="before")
    @classmethod
    def default_blank_language(cls, data):
        if isinstance(data, dict) and not data.get("language_preference"):
            data = {**data, "language_preference": LanguagePreference.english.value}
        return data

    @field_validator("language_preference", mode="before")
    @classmethod
    def parse_language(cls, value):
        if isinstance(value, LanguagePreference):
            return value
        return LanguagePreference(str(value).strip().lower())

    @field_validator("received_at", mode="before")
    @classmethod
    def parse_received_at(cls, value):
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value
