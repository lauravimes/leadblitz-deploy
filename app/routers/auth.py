import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Response, Depends, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.models import User
from app.auth.passwords import hash_password, verify_password
from app.auth.sessions import create_token
from app.services.system_email import send_system_email, build_branded_email

router = APIRouter(tags=["auth"])


def _set_session(response: Response, user: User) -> Response:
    token = create_token(user.id)
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30,
    )
    return response


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = db.query(User).filter(User.email == email.lower().strip()).first()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Invalid email or password"},
        )

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/search"
    return _set_session(response, user)


@router.post("/register")
def register(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    email_clean = email.lower().strip()

    if db.query(User).filter(User.email == email_clean).first():
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "An account with that email already exists"},
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Password must be at least 8 characters"},
        )

    user = User(
        email=email_clean,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Give 200 free trial credits
    from app.services.credits import credit_manager
    credit_manager.add_credits(db, user.id, 200, "Free trial credits")

    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/search"
    return _set_session(response, user)


@router.post("/logout")
def logout():
    response = Response(status_code=200)
    response.headers["HX-Redirect"] = "/login"
    response.delete_cookie("session")
    return response


@router.post("/forgot-password")
def forgot_password(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates
    user = db.query(User).filter(User.email == email.lower().strip()).first()

    # Always show success (don't reveal if email exists)
    if user:
        token = secrets.token_urlsafe(32)
        user.reset_token = token
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
        send_system_email(user.email, "Reset your LeadBlitz password", html)

    return templates.TemplateResponse(
        "partials/error.html",
        {"request": request, "message": "If that email exists, a reset link has been sent."},
    )


@router.post("/reset-password")
def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    templates = request.app.state.templates

    if len(password) < 8:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Password must be at least 8 characters"},
        )

    user = db.query(User).filter(
        User.reset_token == token,
        User.reset_token_expiry > datetime.now(timezone.utc),
    ).first()

    if not user:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": "Invalid or expired reset link"},
        )

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
