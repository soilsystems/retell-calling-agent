import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import FollowupStatus


class Followup(Base):
    __tablename__ = "followups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"))
    call_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_attempts.id", ondelete="SET NULL")
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    zoho_task_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[FollowupStatus] = mapped_column(
        Enum(FollowupStatus, name="followup_status"),
        nullable=False,
        default=FollowupStatus.pending,
    )

    lead = relationship("Lead", back_populates="followups")
    call_attempt = relationship("CallAttempt", back_populates="followups")
