from typing import Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import get_settings

_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret)


def create_token(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def decode_token(token: str) -> Optional[int]:
    try:
        data = _serializer().loads(token, max_age=_MAX_AGE)
        return data.get("uid")
    except (BadSignature, SignatureExpired):
        return None
