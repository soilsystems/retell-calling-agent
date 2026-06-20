from uuid import uuid4

import pytest

from app.models import LanguagePreference, Lead
from app.services import retell_service


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _lead():
    return Lead(
        id=uuid4(),
        zoho_lead_id="z1",
        name="Test",
        phone="+919876543210",
        language_preference=LanguagePreference.english,
    )


@pytest.mark.asyncio
async def test_apply_site_visit_sets_fixed_and_date():
    lead = _lead()
    db = _Db()
    await retell_service._apply_site_visit(
        lead, {"site_visit_agreed": True, "site_visit_day": "Saturday"}, db
    )
    assert lead.site_visit_fixed is True
    assert lead.site_visit_date == "Saturday"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_apply_site_visit_noop_when_not_agreed():
    lead = _lead()
    db = _Db()
    await retell_service._apply_site_visit(lead, {"site_visit_agreed": False}, db)
    assert not lead.site_visit_fixed  # None/False before any agreement
    assert db.commits == 0


@pytest.mark.asyncio
async def test_apply_site_visit_never_unfixes():
    lead = _lead()
    lead.site_visit_fixed = True
    lead.site_visit_date = "Sunday"
    db = _Db()
    # A later call with no agreement must not wipe a previously-fixed visit.
    await retell_service._apply_site_visit(lead, {"site_visit_agreed": False}, db)
    assert lead.site_visit_fixed is True
    assert lead.site_visit_date == "Sunday"
