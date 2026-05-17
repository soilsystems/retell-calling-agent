from datetime import datetime
from zoneinfo import ZoneInfo

from app.utils.business_hours import next_business_slot


def test_sunday_returns_monday_9am():
    dt = datetime(2026, 5, 17, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    slot = next_business_slot(dt).astimezone(ZoneInfo("Asia/Kolkata"))
    assert slot.weekday() == 0
    assert slot.hour == 9
    assert slot.minute == 0


def test_saturday_after_7pm_returns_monday():
    dt = datetime(2026, 5, 16, 19, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
    slot = next_business_slot(dt).astimezone(ZoneInfo("Asia/Kolkata"))
    assert slot.weekday() == 0
    assert slot.hour == 9
    assert slot.minute == 0


def test_weekday_before_9am_returns_same_day_9am():
    dt = datetime(2026, 5, 14, 8, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    slot = next_business_slot(dt).astimezone(ZoneInfo("Asia/Kolkata"))
    assert slot.date() == dt.date()
    assert slot.hour == 9
    assert slot.minute == 0
