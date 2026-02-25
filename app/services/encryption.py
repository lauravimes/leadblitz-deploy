import logging

from cryptography.fernet import Fernet

from app.config import get_settings

logger = logging.getLogger(__name__)

_fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = get_settings().encryption_key
        if not key:
            raise ValueError(
                "ENCRYPTION_KEY environment variable is required. "
                "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plain_text: str) -> str:
    if not plain_text:
        return ""
    return _get_fernet().encrypt(plain_text.encode()).decode()


def decrypt(encrypted_text: str) -> str:
    if not encrypted_text:
        return ""
    try:
        return _get_fernet().decrypt(encrypted_text.encode()).decode()
    except Exception:
        return ""
