import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import LanguagePreference


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_phone", "phone"),
        Index("ix_leads_zoho_lead_id", "zoho_lead_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    zoho_lead_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(16), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    city: Mapped[str | None] = mapped_column(String(120))
    language_preference: Mapped[LanguagePreference] = mapped_column(
        Enum(LanguagePreference, name="language_preference"),
        nullable=False,
        default=LanguagePreference.english,
    )
    source: Mapped[str | None] = mapped_column(String(120))
    campaign: Mapped[str | None] = mapped_column(String(120))
    # Site-visit tracking. site_visit_fixed/date mirror the latest call's
    # structured data (the AI fixed a visit); visited is a manual flag set from
    # the dashboard once the person actually shows up, which triggers a
    # feedback WhatsApp (guarded by feedback_sent).
    site_visit_fixed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    site_visit_date: Mapped[str | None] = mapped_column(String(120))
    visited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    visited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    feedback_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    call_jobs = relationship("CallJob", back_populates="lead", cascade="all, delete-orphan")
    sync_logs = relationship("CrmSyncLog", back_populates="lead")
    followups = relationship("Followup", back_populates="lead")
    whatsapp_logs = relationship("WhatsAppLog", back_populates="lead")
