"""Small shared input validators."""

import re

# Pragmatic address check: one @, a non-empty local part, a dotted domain, no
# whitespace or angle brackets. Not RFC-complete on purpose.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
MAX_EMAIL_LENGTH = 254


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    email = normalize_email(email)
    if not email or len(email) > MAX_EMAIL_LENGTH:
        return False
    return bool(_EMAIL_RE.match(email))
