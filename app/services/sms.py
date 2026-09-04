import logging
import math
import re
from typing import Dict, Optional

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat
from twilio.rest import Client

logger = logging.getLogger(__name__)

_UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)
_UK_HINTS = ("uk", "united kingdom", "england", "scotland", "wales", "northern ireland")

GSM7_SEGMENT = 160
GSM7_CONCAT_SEGMENT = 153


def validate_sms_config(account_sid: Optional[str] = None, auth_token: Optional[str] = None,
                        phone_number: Optional[str] = None) -> bool:
    return all([account_sid, auth_token, phone_number])


def infer_region(phone: Optional[str], address: Optional[str]) -> str:
    """Default dialling region for a national-format number.

    ``+…`` numbers do not need one. A number starting with ``0`` next to a UK
    address / postcode is ``GB``; everything else falls back to ``US``.
    """
    raw = (phone or "").strip()
    if raw.startswith("0"):
        addr = (address or "").lower()
        parts = [p.strip() for p in addr.split(",")]
        if _UK_POSTCODE.search(address or "") or any(p in _UK_HINTS for p in parts):
            return "GB"
    return "US"


def normalize_phone(raw: Optional[str], default_region: str = "US") -> Optional[str]:
    """Return the number in E.164 (``+441189571234``) or ``None`` when it cannot be
    parsed as a valid number — Twilio rejects national formats outright."""
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw.strip(), (default_region or "US").upper())
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def sms_segments(message: str) -> int:
    """How many SMS segments a message will be billed as (GSM-7 approximation)."""
    n = len(message or "")
    if n <= GSM7_SEGMENT:
        return 1 if n else 0
    return math.ceil(n / GSM7_CONCAT_SEGMENT)


def send_sms(to_phone: str, message: str, account_sid: str, auth_token: str, phone_number: str) -> Dict:
    """Send one SMS. ``auth_token`` must already be decrypted; ``to_phone`` must be E.164."""
    if not validate_sms_config(account_sid, auth_token, phone_number):
        return {"success": False, "error": "Twilio configuration incomplete."}
    if not to_phone:
        return {"success": False, "error": "Recipient phone number is required."}
    try:
        client = Client(account_sid, auth_token)
        sms_message = client.messages.create(body=message, from_=phone_number, to=to_phone)
        return {"success": True, "message_sid": sms_message.sid, "status": sms_message.status}
    except Exception as e:  # noqa: BLE001 — Twilio raises many exception types
        logger.warning("[sms] Twilio send to %s failed: %s", to_phone, e)
        return {"success": False, "error": str(e)}
