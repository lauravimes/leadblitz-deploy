from typing import Optional

from fastapi import Request, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session) -> User:
    """Extract user from signed session cookie. Raises 401 if invalid."""
    from app.auth.sessions import decode_token

    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_id = decode_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid session")

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def get_optional_user(request: Request, db: Session) -> Optional[User]:
    """Like get_current_user but returns None instead of raising."""
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def require_admin(request: Request, db: Session) -> User:
    """Require the current user to be an admin. Raises 403 if not."""
    user = get_current_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
