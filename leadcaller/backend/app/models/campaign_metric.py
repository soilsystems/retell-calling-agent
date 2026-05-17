from datetime import date

from sqlalchemy import Date, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CampaignMetric(Base):
    __tablename__ = "campaign_metrics"

    campaign: Mapped[str] = mapped_column(Text, primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_leads: Mapped[int] = mapped_column(Integer, nullable=False)
    calls_made: Mapped[int] = mapped_column(Integer, nullable=False)
    answered: Mapped[int] = mapped_column(Integer, nullable=False)
    hot_leads: Mapped[int] = mapped_column(Integer, nullable=False)
    conversion_rate: Mapped[float] = mapped_column(Float, nullable=False)
