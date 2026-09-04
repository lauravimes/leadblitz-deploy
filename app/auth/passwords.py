import bcrypt

# bcrypt only uses the first 72 bytes of a password; bcrypt >= 5 raises instead of
# silently truncating. Enforce the limit explicitly so users get a form error.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8


def password_error(plain: str) -> str | None:
    """Return a user-facing validation message, or None if the password is acceptable."""
    if len(plain) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(plain.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"Password must be {MAX_PASSWORD_BYTES} characters or fewer"
    return None


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    if len(plain.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode())
    except ValueError:
        return False
