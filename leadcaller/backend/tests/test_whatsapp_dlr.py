"""Tests for WhatsApp delivery-receipt (DLR) classification + status apply."""
from types import SimpleNamespace

import pytest

from app.services import exotel_whatsapp_service as wa


def _dlr(**msg):
    return {"whatsapp": {"messages": [msg]}}


def test_extract_dlr_failure_from_exotel_detailed_status():
    # Real Exotel DLR shape captured from prod (template header mismatch).
    payload = _dlr(
        sid="727c690f9aef4d467d1f791448811a6j",
        exo_detailed_status="EX_TEMPLATE_PARAM_ERROR",
        description="header: Format mismatch, expected DOCUMENT, received UNKNOWN",
        callback_type="dlr",
        exo_status_code=30023,
    )
    sid, status, detail = wa.extract_dlr(payload)
    assert sid == "727c690f9aef4d467d1f791448811a6j"
    assert status == "failed"
    assert "EX_TEMPLATE_PARAM_ERROR" in (detail or "")


@pytest.mark.parametrize(
    "msg,expected",
    [
        ({"sid": "a", "status": "delivered"}, "delivered"),
        ({"sid": "b", "status": "read"}, "read"),
        ({"sid": "c", "status": "undelivered"}, "failed"),
        # Real Exotel exo_detailed_status taxonomy (success ladder + failures).
        ({"sid": "s", "exo_detailed_status": "EX_MESSAGE_SENT", "exo_status_code": 30001}, "sent"),
        ({"sid": "d2", "exo_detailed_status": "EX_MESSAGE_DELIVERED", "exo_status_code": 30002}, "delivered"),
        ({"sid": "seen", "exo_detailed_status": "EX_MESSAGE_SEEN", "exo_status_code": 30003}, "read"),
        ({"sid": "re", "exo_detailed_status": "EX_REENGAGEMENT_ERROR", "exo_status_code": 30018}, "failed"),
        ({"sid": "f", "exo_detailed_status": "EX_RESTRICTED_BY_META"}, "failed"),
        # A 30xxx code alone is NOT enough to call it failed (30001/30003 are success).
        ({"sid": "g", "exo_status_code": 30007}, None),
        ({"sid": "h"}, None),  # nothing to classify → leave unchanged
    ],
)
def test_classify_variants(msg, expected):
    sid, status, _ = wa.extract_dlr(_dlr(**msg))
    assert sid == msg["sid"]
    assert status == expected


@pytest.mark.asyncio
async def test_apply_delivery_status_monotonic():
    msg = SimpleNamespace(status=None, status_detail=None, provider_message_id="m1")

    class FakeResult:
        def scalar_one_or_none(self):
            return msg

    class FakeDb:
        def __init__(self):
            self.commits = 0

        async def execute(self, _stmt):
            return FakeResult()

        async def commit(self):
            self.commits += 1

    db = FakeDb()
    assert await wa.apply_delivery_status(db, "m1", "delivered", None) is True
    assert msg.status == "delivered"
    # A late "sent" must NOT downgrade a delivered message.
    assert await wa.apply_delivery_status(db, "m1", "sent", None) is False
    assert msg.status == "delivered"
    # Read upgrades delivered.
    assert await wa.apply_delivery_status(db, "m1", "read", None) is True
    assert msg.status == "read"
    # failed always wins, and detail is recorded.
    assert await wa.apply_delivery_status(db, "m1", "failed", "EX_RESTRICTED_BY_META") is True
    assert msg.status == "failed"
    assert msg.status_detail == "EX_RESTRICTED_BY_META"


@pytest.mark.asyncio
async def test_apply_delivery_status_noops():
    class FakeDb:
        async def execute(self, _stmt):
            class R:
                def scalar_one_or_none(self):
                    return None
            return R()

        async def commit(self):
            raise AssertionError("should not commit when nothing matched")

    db = FakeDb()
    # No provider id, no status, or no matching row → no update, no commit.
    assert await wa.apply_delivery_status(db, None, "delivered", None) is False
    assert await wa.apply_delivery_status(db, "x", None, None) is False
    assert await wa.apply_delivery_status(db, "missing", "delivered", None) is False
