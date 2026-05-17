import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, Index, String, Text
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    call_jobs = relationship("CallJob", back_populates="lead", cascade="all, delete-orphan")
    sync_logs = relationship("CrmSyncLog", back_populates="lead")
    followups = relationship("Followup", back_populates="lead")
