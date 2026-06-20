from datetime import datetime
from zoneinfo import ZoneInfo

from app.utils.business_hours import next_business_slot, next_twice_daily_slot

IST = ZoneInfo("Asia/Kolkata")


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


# ── Twice-daily retry slots (10am / 2pm IST) ────────────────────────────────

def test_twice_daily_before_10am_returns_today_10am():
    dt = datetime(2026, 5, 14, 8, 0, tzinfo=IST)  # Thursday
    slot = next_twice_daily_slot(dt).astimezone(IST)
    assert slot.date() == dt.date()
    assert (slot.hour, slot.minute) == (10, 0)


def test_twice_daily_between_slots_returns_today_2pm():
    dt = datetime(2026, 5, 14, 11, 30, tzinfo=IST)
    slot = next_twice_daily_slot(dt).astimezone(IST)
    assert slot.date() == dt.date()
    assert (slot.hour, slot.minute) == (14, 0)


def test_twice_daily_after_2pm_returns_next_day_10am():
    dt = datetime(2026, 5, 14, 15, 0, tzinfo=IST)  # Thursday afternoon
    slot = next_twice_daily_slot(dt).astimezone(IST)
    assert slot.date() == datetime(2026, 5, 15, 0, 0, tzinfo=IST).date()
    assert (slot.hour, slot.minute) == (10, 0)


def test_twice_daily_skips_sunday():
    dt = datetime(2026, 5, 16, 15, 0, tzinfo=IST)  # Saturday after 2pm
    slot = next_twice_daily_slot(dt).astimezone(IST)
    assert slot.weekday() == 0  # Monday, not Sunday
    assert (slot.hour, slot.minute) == (10, 0)
