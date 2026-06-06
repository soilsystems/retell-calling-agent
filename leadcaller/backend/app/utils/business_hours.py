from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytz

BUSINESS_TZ = "Asia/Kolkata"
BUSINESS_START = time(9, 0)
BUSINESS_END = time(19, 0)
BUSINESS_DAYS = {0, 1, 2, 3, 4, 5}


def _as_business_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(BUSINESS_TZ))


def is_business_hours(dt: datetime) -> bool:
    local_dt = _as_business_tz(dt)
    return (
        local_dt.weekday() in BUSINESS_DAYS
        and BUSINESS_START <= local_dt.time() < BUSINESS_END
    )


def next_business_slot(dt: datetime) -> datetime:
    local_dt = _as_business_tz(dt)

    if local_dt.weekday() not in BUSINESS_DAYS:
        days_until_monday = (7 - local_dt.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 1
        next_day = local_dt.date() + timedelta(days=days_until_monday)
        return datetime.combine(next_day, BUSINESS_START, ZoneInfo(BUSINESS_TZ)).astimezone(timezone.utc)

    if local_dt.time() < BUSINESS_START:
        return datetime.combine(local_dt.date(), BUSINESS_START, ZoneInfo(BUSINESS_TZ)).astimezone(timezone.utc)

    if local_dt.time() >= BUSINESS_END:
        next_day = local_dt + timedelta(days=1)
        while next_day.weekday() not in BUSINESS_DAYS:
            next_day += timedelta(days=1)
        return datetime.combine(next_day.date(), BUSINESS_START, ZoneInfo(BUSINESS_TZ)).astimezone(timezone.utc)

    return local_dt.astimezone(timezone.utc)


def get_next_business_day_at_10am() -> datetime:
    ist = pytz.timezone(BUSINESS_TZ)
    now_ist = datetime.now(ist)
    next_day = now_ist + timedelta(days=1)

    while next_day.weekday() == 6:
        next_day += timedelta(days=1)

    next_call = next_day.replace(hour=10, minute=0, second=0, microsecond=0)
    return next_call.astimezone(pytz.utc).replace(tzinfo=None)
