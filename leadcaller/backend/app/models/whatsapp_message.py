"""Per-message storage for the two-way WhatsApp chat UI.

WhatsAppLog tracks template-send attempts tied to call_attempts (campaign analytics).
WhatsAppMessage is a flat conversation log: every inbound and outbound message,
keyed by phone number, used to render chat threads in the dashboard.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Enum, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.enums import WhatsAppMessageDirection, WhatsAppMessageType


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    direction: Mapped[WhatsAppMessageDirection] = mapped_column(
        Enum(WhatsAppMessageDirection, name="whatsapp_message_direction"), nullable=False
    )
    message_type: Mapped[WhatsAppMessageType] = mapped_column(
        Enum(WhatsAppMessageType, name="whatsapp_message_type"), nullable=False
    )
    body: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    media_filename: Mapped[str | None] = mapped_column(Text)
    media_caption: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[str | None] = mapped_column(Text)
    longitude: Mapped[str | None] = mapped_column(Text)
    location_name: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    # Delivery state from Exotel DLR callbacks: sent | delivered | read | failed.
    # status_detail holds Exotel's exo_detailed_status / description for failures.
    status: Mapped[str | None] = mapped_column(Text)
    status_detail: Mapped[str | None] = mapped_column(Text)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
