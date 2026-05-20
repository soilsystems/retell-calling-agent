import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import CallAttemptStatus


class CallAttempt(Base):
    __tablename__ = "call_attempts"
    __table_args__ = (Index("ix_call_attempts_retell_call_id", "retell_call_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_jobs.id", ondelete="CASCADE"), nullable=False
    )
    retell_call_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[CallAttemptStatus] = mapped_column(
        Enum(CallAttemptStatus, name="call_attempt_status"),
        nullable=False,
        default=CallAttemptStatus.initiated,
    )
    recording_url: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    structured_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    call_job = relationship("CallJob", back_populates="attempts")
    followups = relationship("Followup", back_populates="call_attempt")
    whatsapp_logs = relationship("WhatsAppLog", back_populates="call_attempt")
