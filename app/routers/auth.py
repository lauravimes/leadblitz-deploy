import hashlib
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Request, Response, Depends, Form
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.deps import get_db, get_current_user
from app.models import User
from app.auth.passwords import hash_password, verify_password, password_error
from app.auth.sessions import create_token
from app.services.rate_limit import (
    client_ip,
    login_limiter,
    register_limiter,
    forgot_password_limiter,
)
from app.services.system_email import send_system_email, build_branded_email
from app.validation import is_valid_email, normalize_email

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

FREE_TRIAL_CREDITS = 200

# Domains where multiple signups are expected (free email providers)
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "zoho.com", "yandex.com",
    "gmx.com", "gmx.net", "fastmail.com", "tutanota.com", "hey.com",
}

# A hash to verify against when the email is unknown, so login timing does not
# reveal whether an account exists.
_DUMMY_HASH = hash_password("not-a-real-password-just-for-timing")


def canonical_email(email: str) -> str:
    """Collapse Gmail dot/plus aliases so foo+1@gmail.com and f.o.o@gmail.com are
    treated as one address for trial-credit dedup."""
    email = normalize_email(email)
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def _should_grant_free_credits(db: Session, email: str, ip: Optional[str], exclude_user_id: int = None) -> bool:
    """Check whether a new signup should receive free trial credits.

    Returns False if:
    - A non-public email domain already has an existing account (domain dedup)
    - The same canonical address (Gmail alias collapsed) already exists
    - The same IP registered another account in the last 30 days (IP dedup)
    """
    domain = email.rsplit("@", 1)[-1].lower()

    # Layer 1: email domain dedup (skip for public providers)
    if domain not in PUBLIC_EMAIL_DOMAINS:
        q = db.query(User.id).filter(func.lower(User.email).like(f"%@{domain}"))
        if exclude_user_id:
            q = q.filter(User.id != exclude_user_id)
        if q.first():
            return False

    # Layer 1b: alias dedup for public providers (foo+1@gmail.com)
    canon = canonical_email(email)
    local, _, canon_domain = canon.partition("@")
    if local and canon_domain in ("gmail.com",):
        candidates = (
            db.query(User.id, User.email)
            .filter(func.lower(User.email).like(f"%@{domain}"))
        )
        if exclude_user_id:
            candidates = candidates.filter(User.id != exclude_user_id)
        for _uid, other in candidates.all():
            if canonical_email(other) == canon:
                return False

    # Layer 2: IP dedup (same IP signed up in last 30 days)
    if ip and ip != "unknown":
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        q = (
            db.query(User.id)
            .filter(User.signup_ip == ip, User.created_at >= cutoff)
        )
        if exclude_user_id:
            q = q.filter(User.id != exclude_user_id)
        if q.first():
            return False

    return True


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _set_session(request: Request, response: Response, user: User) -> Response:
    token = create_token(user)
    response.set_cookie(
        "session",
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=60 * 60 * 24 * 30,
    )
    return response


def _error(request: Request, message: str, status_code: int = 200):
    return request.app.state.templates.TemplateResponse(
        "partials/error.html",
        {"request": request, "message": message},
        status_code=status_code,
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    if not login_limiter.allow(f"login:{ip}"):
        return _error(request, "Too many login attempts. Please wait a minute and try again.", 429)

    email_clean = normalize_email(email)
    user = db.query(User).filter(User.email == email_clean).first()

    # Always run a bcrypt check so unknown emails take as long as wrong passwords.
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok or not user.is_active:
        return _error(request, "Invalid email or password")

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/search"
    return _set_session(request, response, user)


@router.post("/register")
def register(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    if not register_limiter.allow(f"register:{ip}"):
        return _error(request, "Too many sign-ups from this network. Please try again later.", 429)

    email_clean = normalize_email(email)
    if not is_valid_email(email_clean):
        return _error(request, "Please enter a valid email address")

    full_name = (full_name or "").strip()[:255]
    if not full_name:
        return _error(request, "Please enter your name")

    pw_err = password_error(password)
    if pw_err:
        return _error(request, pw_err)

    if db.query(User).filter(User.email == email_clean).first():
        return _error(request, "An account with that email already exists")

    client_addr = ip if ip != "unknown" else None

    user = User(
        email=email_clean,
        password_hash=hash_password(password),
        full_name=full_name,
        signup_ip=client_addr,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _error(request, "An account with that email already exists")
    db.refresh(user)

    # Give free trial credits — unless abuse detected
    from app.services.credits import credit_manager
    granted = _should_grant_free_credits(db, email_clean, client_addr, exclude_user_id=user.id)
    if granted:
        credit_manager.add_credits(db, user.id, FREE_TRIAL_CREDITS, "Free trial credits")
        redirect = "/search"
    else:
        logger.info("Free trial credits withheld for user %s (%s, ip=%s)", user.id, email_clean, client_addr)
        redirect = "/credits?trial=withheld"

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = redirect
    return _set_session(request, response, user)


@router.post("/logout")
def logout():
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/login"
    response.delete_cookie("session")
    return response


@router.get("/logout")
def logout_get():
    # Side-effect-free: GET only points at the login page; logging out requires a POST.
    return RedirectResponse("/login", status_code=302)


@router.post("/forgot-password")
def forgot_password(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = client_ip(request)
    if not forgot_password_limiter.allow(f"forgot:{ip}"):
        return _error(request, "Too many reset requests. Please try again later.", 429)

    user = db.query(User).filter(User.email == normalize_email(email)).first()

    # Always show the same message and do the same amount of work (email goes out
    # in the background) so the response does not reveal whether the email exists.
    if user:
        token = secrets.token_urlsafe(32)
        # Only the hash is stored, so a database read never yields a usable link.
        user.reset_token = _hash_reset_token(token)
        user.reset_token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()

        base_url = str(request.base_url).rstrip("/")
        reset_url = f"{base_url}/reset-password?token={token}"

        html = build_branded_email(
            heading="Reset your password",
            body_content="<p>We received a request to reset your password. Click the button below to set a new one. This link expires in 1 hour.</p>",
            button_text="Reset Password",
            button_url=reset_url,
            footer_note="If you didn't request this, you can safely ignore this email.",
        )
        background_tasks.add_task(send_system_email, user.email, "Reset your LeadBlitz password", html)

    return _error(request, "If that email exists, a reset link has been sent.")


@router.post("/reset-password")
def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    pw_err = password_error(password)
    if pw_err:
        return _error(request, pw_err)

    user = db.query(User).filter(
        User.reset_token == _hash_reset_token(token),
        User.reset_token_expiry > datetime.now(timezone.utc),
    ).first()

    if not user:
        return _error(request, "Invalid or expired reset link")

    user.password_hash = hash_password(password)
    user.reset_token = None
    user.reset_token_expiry = None
    db.commit()

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/login"
    return response


@router.get("/api/auth/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return JSONResponse({
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "is_admin": user.is_admin,
    })
