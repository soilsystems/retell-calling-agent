"""Regression tests for manually-placed AI calls surfacing on the dashboard.

A manual "Call a number" / per-lead Call must record an in_progress CallJob and
bump the lead at dial time so the lead jumps to the top of the dashboard
immediately — and must NOT leave duplicate pending jobs (which would make the
scheduler double-dial the lead) or an orphaned in_progress job if the dial fails.
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models import CallJob, CallJobStatus
from app.routers import admin


class FakeDb:
    def __init__(self):
        self.added = []
        self.executed = []
        self.commits = 0

    async def execute(self, stmt):
        self.executed.append(stmt)
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return None


def _created_job(db):
    return next(o for o in db.added if isinstance(o, CallJob))


@pytest.mark.asyncio
async def test_record_manual_dial_creates_in_progress_job_and_bumps_lead():
    lead = SimpleNamespace(id=uuid4(), updated_at=None)
    db = FakeDb()

    job = await admin._record_manual_dial(lead, db)

    # A fresh in_progress job is created and attributed to this lead.
    assert job.status == CallJobStatus.in_progress
    assert job.trigger_reason == "manual_call"
    assert job.lead_id == lead.id
    assert job.started_at is not None
    # Lead.updated_at is bumped so the backend /admin/leads ranking includes it.
    assert lead.updated_at is not None
    # Exactly one statement runs before the insert: the cancel of pending jobs.
    assert len(db.executed) == 1
    assert type(db.executed[0]).__name__ == "Update"
    assert db.commits >= 1


@pytest.mark.asyncio
async def test_place_manual_ai_call_marks_job_failed_on_dial_error(monkeypatch):
    lead = SimpleNamespace(id=uuid4(), updated_at=None)
    db = FakeDb()

    async def boom(_lead, _db):
        raise HTTPException(status_code=502, detail="exotel down")

    monkeypatch.setattr(admin, "connect_exotel_call_with_retell_ai", boom)

    with pytest.raises(HTTPException):
        await admin._place_manual_ai_call(lead, db)

    # The dial failed → the job must not be left orphaned as in_progress.
    assert _created_job(db).status == CallJobStatus.failed


@pytest.mark.asyncio
async def test_place_manual_ai_call_keeps_job_in_progress_on_success(monkeypatch):
    lead = SimpleNamespace(id=uuid4(), updated_at=None)
    db = FakeDb()

    async def ok(_lead, _db):
        return {"mode": "ai", "status": "queued"}

    monkeypatch.setattr(admin, "connect_exotel_call_with_retell_ai", ok)

    result = await admin._place_manual_ai_call(lead, db)

    assert result["status"] == "queued"
    assert _created_job(db).status == CallJobStatus.in_progress
