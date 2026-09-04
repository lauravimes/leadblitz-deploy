import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger(__name__)

_fernet = None


def is_configured() -> bool:
    return bool(get_settings().encryption_key)


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
    """Decrypt a stored secret.

    Returns "" only when the ciphertext is genuinely undecryptable with the current
    key (InvalidToken). A missing ENCRYPTION_KEY raises so misconfiguration is loud
    instead of silently turning every stored credential into an empty string.
    """
    if not encrypted_text:
        return ""
    try:
        return _get_fernet().decrypt(encrypted_text.encode()).decode()
    except InvalidToken:
        logger.error("Stored secret could not be decrypted — ENCRYPTION_KEY may have been rotated")
        return ""
