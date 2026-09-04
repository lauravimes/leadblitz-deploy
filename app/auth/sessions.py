import hashlib
from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import get_settings

_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="leadblitz-session")


def password_version(password_hash: str) -> str:
    """Short fingerprint of the password hash. Baked into the session token so that
    changing or resetting the password invalidates every existing session."""
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


def create_token(user) -> str:
    return _serializer().dumps({"uid": user.id, "pv": password_version(user.password_hash)})


def decode_token(token: str) -> Optional[dict]:
    """Return the token payload ({"uid", "pv"}) or None if invalid/expired."""
    try:
        data = _serializer().loads(token, max_age=_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or "uid" not in data:
        return None
    return data
