import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import WhatsAppLogStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WhatsAppLog(Base):
    __tablename__ = "whatsapp_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    call_attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_attempts.id", ondelete="CASCADE"), nullable=False
    )
    phone: Mapped[str | None] = mapped_column(Text)
    template_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[WhatsAppLogStatus] = mapped_column(
        Enum(WhatsAppLogStatus, name="whatsapp_log_status"),
        nullable=False,
    )
    wati_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lead = relationship("Lead", back_populates="whatsapp_logs")
    call_attempt = relationship("CallAttempt", back_populates="whatsapp_logs")
