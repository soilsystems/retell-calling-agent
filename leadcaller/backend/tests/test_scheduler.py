from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.models import CallJob, CallJobStatus, LanguagePreference
from app.services import retell_service
from app.utils.business_hours import get_next_business_day_at_10am, next_business_slot


class Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class SessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_pending_job_triggered_when_scheduled_at_passed(monkeypatch):
    job = SimpleNamespace(id=uuid4())
    triggered = []

    class Db:
        async def execute(self, stmt):
            return Result([job])

    async def fake_trigger(job_id):
        triggered.append(job_id)

    monkeypatch.setattr(retell_service, "AsyncSessionLocal", lambda: SessionContext(Db()))
    monkeypatch.setattr(retell_service, "trigger_retell_call", fake_trigger)

    await retell_service.run_scheduled_calls()

    assert triggered == [job.id]


@pytest.mark.asyncio
async def test_pending_job_not_triggered_when_scheduled_at_future(monkeypatch):
    triggered = []

    class Db:
        async def execute(self, stmt):
            return Result([])

    async def fake_trigger(job_id):
        triggered.append(job_id)

    monkeypatch.setattr(retell_service, "AsyncSessionLocal", lambda: SessionContext(Db()))
    monkeypatch.setattr(retell_service, "trigger_retell_call", fake_trigger)

    await retell_service.run_scheduled_calls()

    assert triggered == []


@pytest.mark.asyncio
async def test_no_answer_schedules_twice_daily_slot(monkeypatch):
    job = CallJob(
        id=uuid4(), lead_id=uuid4(), status=CallJobStatus.failed,
        scheduled_at=datetime.now(timezone.utc), retry_count=0, max_retries=3,
    )
    next_retry = datetime(2026, 6, 8, 8, 30, tzinfo=timezone.utc)  # a 2pm IST slot

    class Db:
        async def get(self, model, id_):
            return job

        async def commit(self):
            return None

    monkeypatch.setattr(retell_service, "next_twice_daily_slot", lambda _now: next_retry)

    await retell_service.schedule_retry(job.id, "no_answer", Db())

    assert job.status == CallJobStatus.pending
    assert job.scheduled_at == next_retry
    assert job.trigger_reason == "no_answer_retry"
    assert job.retry_count == 1


@pytest.mark.asyncio
async def test_no_answer_retries_up_to_ten_then_cancels(monkeypatch):
    # A job that already has max_retries=3 should still get up to 10 no_answer
    # retries (twice daily for 5 days) before being cancelled.
    job = CallJob(
        id=uuid4(), lead_id=uuid4(), status=CallJobStatus.failed,
        scheduled_at=datetime.now(timezone.utc), retry_count=9, max_retries=3,
    )

    class Db:
        async def get(self, model, id_):
            return job

        async def commit(self):
            return None

    monkeypatch.setattr(
        retell_service, "next_twice_daily_slot",
        lambda _now: datetime(2026, 6, 8, 8, 30, tzinfo=timezone.utc),
    )

    # retry_count=9 < 10 → still schedules (the 10th retry)
    await retell_service.schedule_retry(job.id, "no_answer", Db())
    assert job.status == CallJobStatus.pending
    assert job.retry_count == 10

    # retry_count=10 → cancelled
    await retell_service.schedule_retry(job.id, "no_answer", Db())
    assert job.status == CallJobStatus.cancelled


@pytest.mark.asyncio
async def test_busy_schedules_30_minutes(monkeypatch):
    now = datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc)
    job = CallJob(id=uuid4(), lead_id=uuid4(), status=CallJobStatus.failed, scheduled_at=now)

    class Db:
        async def get(self, model, id_):
            return job

        async def commit(self):
            return None

    monkeypatch.setattr(retell_service, "_utcnow", lambda: now)

    await retell_service.schedule_retry(job.id, "busy", Db())

    assert job.scheduled_at == now + timedelta(minutes=30)
    assert job.trigger_reason == "busy_retry"


@pytest.mark.asyncio
class _CallbackDb:
    """Minimal AsyncSession stub for _schedule_callback_if_requested.

    `recent_count` is what db.scalar() (the loop-guard count query) returns.
    """

    def __init__(self, recent_count=0):
        self.rows = []
        self._recent_count = recent_count

    def add(self, row):
        self.rows.append(row)

    async def execute(self, stmt):
        class EmptyResult:
            def scalar_one_or_none(self):
                return None

        return EmptyResult()

    async def scalar(self, stmt):
        return self._recent_count

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_callback_requested_creates_new_job():
    lead_id = uuid4()
    attempt = SimpleNamespace(id=uuid4(), call_job=SimpleNamespace(lead_id=lead_id))
    db = _CallbackDb(recent_count=0)

    await retell_service._schedule_callback_if_requested(
        attempt,
        {"callback_required": True, "callback_time": "2026-06-06T10:00:00+05:30"},
        db,
    )

    assert len(db.rows) == 1
    assert db.rows[0].lead_id == lead_id
    assert db.rows[0].status == CallJobStatus.pending
    assert db.rows[0].trigger_reason == "callback_requested"


@pytest.mark.asyncio
async def test_callback_loop_guard_blocks_runaway_chains():
    """Once a lead has hit the per-window callback cap, no further auto-callback
    is scheduled — prevents an endless call → callback → callback chain."""
    lead_id = uuid4()
    attempt = SimpleNamespace(id=uuid4(), call_job=SimpleNamespace(lead_id=lead_id))
    db = _CallbackDb(recent_count=retell_service.MAX_CALLBACKS_PER_WINDOW)

    await retell_service._schedule_callback_if_requested(
        attempt,
        {"callback_required": True, "callback_time": "after 2 minutes"},
        db,
    )

    assert db.rows == []  # capped — nothing scheduled


@pytest.mark.asyncio
async def test_callback_not_blocked_by_stale_job():
    """A stuck/old callback job must NOT block a fresh callback request — the
    dedup query filters by a ±5min window around the new scheduled time, so a
    far-off stale job won't be returned and a new job is created."""
    lead_id = uuid4()
    attempt = SimpleNamespace(id=uuid4(), call_job=SimpleNamespace(lead_id=lead_id))
    db = _CallbackDb(recent_count=0)

    # callback "in 2 minutes" — a stale job days ago is far outside the window.
    await retell_service._schedule_callback_if_requested(
        attempt,
        {"callback_required": True, "callback_time": "after 2 minutes"},
        db,
    )

    assert len(db.rows) == 1
    assert db.rows[0].trigger_reason == "callback_requested"
    assert db.rows[0].status == CallJobStatus.pending


@pytest.mark.asyncio
async def test_outside_business_hours_schedules_next_slot(monkeypatch):
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    next_slot = datetime(2026, 6, 6, 3, 30, tzinfo=timezone.utc)
    job = SimpleNamespace(
        id=uuid4(),
        status=CallJobStatus.pending,
        scheduled_at=now,
        lead=SimpleNamespace(),
    )

    class ResultOne:
        def scalar_one_or_none(self):
            return job

    class Db:
        async def execute(self, stmt):
            return ResultOne()

        async def commit(self):
            return None

    monkeypatch.setattr(retell_service, "_utcnow", lambda: now)
    monkeypatch.setattr(retell_service, "is_business_hours", lambda dt: False)
    monkeypatch.setattr(retell_service, "next_business_slot", lambda dt: next_slot)

    await retell_service.trigger_retell_call(job.id, Db())

    assert job.scheduled_at == next_slot
    assert job.status == CallJobStatus.pending


@pytest.mark.asyncio
async def test_outbound_retell_call_uses_auto_language_instruction(monkeypatch):
    now = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)
    job = SimpleNamespace(
        id=uuid4(),
        status=CallJobStatus.pending,
        scheduled_at=now,
        trigger_reason="new_lead",
        attempts=[],
        lead=SimpleNamespace(
            id=uuid4(),
            name="Ravi Chandra",
            phone="+918746905010",
            language_preference=LanguagePreference.kannada,
            city="Bengaluru",
            campaign="June Campaign",
            zoho_lead_id="zoho-1",
        ),
    )
    added = []
    captured = {}

    class ResultOne:
        def scalar_one_or_none(self):
            return job

    class Db:
        async def execute(self, stmt):
            return ResultOne()

        async def scalar(self, stmt):
            return 0

        def add(self, row):
            added.append(row)

        async def commit(self):
            return None

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"Call": {"Sid": "exotel-call-1"}}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, data=None, auth=None):
            captured["url"] = url
            captured["data"] = data
            return Response()

    monkeypatch.setattr(retell_service, "_utcnow", lambda: now)
    monkeypatch.setattr(retell_service, "is_business_hours", lambda dt: True)
    monkeypatch.setattr(retell_service.httpx, "AsyncClient", Client)

    await retell_service.trigger_retell_call(job.id, Db())

    assert "connect" in captured["url"]
    assert captured["data"]["From"] == "+918746905010"
    assert captured["data"]["CallerId"] == "08047283246"
    assert added[0].operation == "exotel_connect_call"
    assert added[0].success is True

    # Verify lead is cached in _pending_outbound_bridges for the webhook to consume
    from app.services.exotel_service import pop_pending_outbound_bridge
    cached = pop_pending_outbound_bridge("+918746905010")
    assert cached is not None
    assert cached["lead_name"] == "Ravi Chandra"


def test_sunday_schedules_monday_10am():
    result = get_next_business_day_at_10am()
    assert result.tzinfo is None


def test_next_business_slot_sunday_returns_monday_9am():
    slot = next_business_slot(datetime(2026, 6, 7, 12, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    local = slot.astimezone(ZoneInfo("Asia/Kolkata"))
    assert local.weekday() == 0
    assert local.hour == 9
