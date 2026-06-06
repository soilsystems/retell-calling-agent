from enum import StrEnum


class LanguagePreference(StrEnum):
    hindi = "hindi"
    english = "english"
    kannada = "kannada"


class CallJobStatus(StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class CallAttemptStatus(StrEnum):
    initiated = "initiated"
    ringing = "ringing"
    answered = "answered"
    no_answer = "no_answer"
    busy = "busy"
    failed = "failed"
    completed = "completed"


class CallDirection(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class WebhookSource(StrEnum):
    zoho = "zoho"
    retell = "retell"
    meta = "meta"


class FollowupStatus(StrEnum):
    pending = "pending"
    created = "created"
    failed = "failed"


class WhatsAppLogStatus(StrEnum):
    sent = "sent"
    failed = "failed"
    skipped = "skipped"
