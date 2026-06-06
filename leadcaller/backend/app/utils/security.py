import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone

from app.config import get_settings

logger = logging.getLogger(__name__)


def _verify_hmac(payload: bytes, header_value: str | None, secret: str) -> bool:
    if not header_value:
        return False
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    expected_values = {digest, f"sha256={digest}"}
    return any(hmac.compare_digest(header_value, expected) for expected in expected_values)


def verify_zoho_signature(payload: bytes, header_token: str | None) -> bool:
    return _verify_hmac(payload, header_token, get_settings().ZOHO_WEBHOOK_SECRET)


def verify_retell_signature(payload: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False

    settings = get_settings()
    match = re.fullmatch(r"v=(\d+),d=([0-9a-fA-F]+)", signature_header.strip())
    if not match:
        return _verify_hmac(payload, signature_header, settings.RETELL_WEBHOOK_SECRET)

    timestamp_ms = int(match.group(1))
    digest = match.group(2)
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if abs(now_ms - timestamp_ms) > 5 * 60 * 1000:
        return False

    signed_payload = payload + str(timestamp_ms).encode("utf-8")
    expected = hmac.new(
        settings.RETELL_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, digest)


def verify_meta_signature(payload: bytes, signature_header: str | None) -> bool:
    settings = get_settings()
    if settings.ENVIRONMENT == "dev" and not signature_header:
        logger.warning("[Meta] No signature header - bypassing in dev mode")
        return True

    if not settings.META_APP_SECRET:
        logger.warning("[Meta] META_APP_SECRET not configured")
        if settings.ENVIRONMENT != "prod":
            return True
        return False

    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("[Meta] Bad signature format: %s", (signature_header or "")[:20])
        return False

    expected = "sha256=" + hmac.new(
        settings.META_APP_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    result = hmac.compare_digest(expected, signature_header)
    if not result:
        logger.warning("[Meta] Signature mismatch")
    return result


def generate_idempotency_key(zoho_lead_id: str, timestamp: datetime) -> str:
    rounded = timestamp.replace(second=0, microsecond=0)
    raw = f"{zoho_lead_id}:{rounded.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
