from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RetellStructuredData(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    interest_level: str = Field(pattern="^(Hot|Warm|Cold|Not Interested)$")
    budget: str | None = None
    timeline: str | None = None
    property_type: str | None = None
    caller_name: str | None = None
    caller_email: str | None = None
    caller_city: str | None = None
    caller_requirement: str | None = None
    caller_details: str | None = None
    language: str | None = None
    follow_up_required: bool = False
    follow_up_time: datetime | None = None
    site_visit_agreed: bool = False
    site_visit_day: str | None = None
    callback_required: bool = False
    callback_time: datetime | None = None


class RetellCallCompletedWebhook(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    call_id: str
    event: str | None = None
    call_status: str = "completed"
    transcript: str | None = None
    summary: str | None = None
    recording_url: str | None = None
    duration_seconds: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    direction: str = "outbound"
    from_number: str | None = None
    to_number: str | None = None
    structured_data: RetellStructuredData | dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def flatten_retell_event(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "call" not in data:
            return data

        call = data.get("call") or {}
        call_analysis = call.get("call_analysis") or {}
        structured_data = (
            call_analysis.get("custom_analysis_data")
            or call_analysis.get("structured_data")
            or call.get("structured_data")
            or {}
        )

        return {
            "call_id": call.get("call_id"),
            "event": data.get("event"),
            "call_status": call.get("call_status") or data.get("event") or "completed",
            "transcript": call.get("transcript"),
            "summary": call_analysis.get("call_summary") or call.get("summary"),
            "recording_url": call.get("recording_url"),
            "duration_seconds": _duration_seconds(call),
            "started_at": _datetime_from_retell(call.get("start_timestamp")),
            "ended_at": _datetime_from_retell(call.get("end_timestamp")),
            "direction": call.get("direction") or call.get("call_direction") or "outbound",
            "from_number": call.get("from_number"),
            "to_number": call.get("to_number"),
            "structured_data": structured_data,
            "metadata": call.get("metadata"),
        }


def _datetime_from_retell(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _duration_seconds(call: dict[str, Any]) -> int | None:
    duration_ms = call.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        return int(duration_ms / 1000)

    start = call.get("start_timestamp")
    end = call.get("end_timestamp")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end >= start:
        return int((end - start) / 1000)
    return call.get("duration_seconds")
