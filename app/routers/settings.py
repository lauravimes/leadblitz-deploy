import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.deps import get_db, get_current_user
from app.config import get_settings
from app.models import User, UserAPIKeys, EmailSettings
from app.auth.passwords import hash_password, verify_password
from app.services.encryption import encrypt, decrypt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])


# --- Profile ---

@router.post("/api/settings/profile")
def update_profile(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    email_clean = email.lower().strip()

    if email_clean != user.email:
        existing = db.query(User).filter(User.email == email_clean).first()
        if existing:
            return HTMLResponse('<div class="error-msg">Email already in use</div>')

    user.full_name = full_name.strip()
    user.email = email_clean
    db.commit()
    return HTMLResponse('<span class="saved-flash">Profile updated</span>')


@router.post("/api/settings/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)

    if not verify_password(current_password, user.password_hash):
        return HTMLResponse('<div class="error-msg">Current password is incorrect</div>')

    if len(new_password) < 8:
        return HTMLResponse('<div class="error-msg">New password must be at least 8 characters</div>')

    user.password_hash = hash_password(new_password)
    db.commit()
    return HTMLResponse('<span class="saved-flash">Password updated</span>')


# --- API Keys ---

@router.get("/api/settings/api-keys")
def get_api_keys(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    keys = db.query(UserAPIKeys).filter_by(user_id=user.id).first()
    return JSONResponse({
        "twilio_account_sid": keys.twilio_account_sid or "" if keys else "",
        "twilio_phone_number": keys.twilio_phone_number or "" if keys else "",
        "hunter_api_key": (keys.hunter_api_key[:8] + "...") if keys and keys.hunter_api_key else "",
        "has_twilio": bool(keys and keys.twilio_account_sid and keys.twilio_auth_token),
        "has_hunter": bool(keys and keys.hunter_api_key),
    })


@router.post("/api/settings/api-keys")
def save_api_keys(
    request: Request,
    twilio_account_sid: str = Form(""),
    twilio_auth_token: str = Form(""),
    twilio_phone_number: str = Form(""),
    hunter_api_key: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    keys = db.query(UserAPIKeys).filter_by(user_id=user.id).first()
    if not keys:
        keys = UserAPIKeys(user_id=user.id)
        db.add(keys)

    if twilio_account_sid:
        keys.twilio_account_sid = twilio_account_sid.strip()
    if twilio_auth_token:
        keys.twilio_auth_token = encrypt(twilio_auth_token.strip())
    if twilio_phone_number:
        keys.twilio_phone_number = twilio_phone_number.strip()
    if hunter_api_key:
        keys.hunter_api_key = encrypt(hunter_api_key.strip())

    db.commit()
    return HTMLResponse('<span class="saved-flash">API keys saved</span>')


# --- Email Provider: SMTP ---

@router.post("/api/settings/email/smtp")
def save_smtp(
    request: Request,
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_username: str = Form(...),
    smtp_password: str = Form(...),
    smtp_from_email: str = Form(...),
    smtp_use_tls: bool = Form(True),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    es = db.query(EmailSettings).filter_by(user_id=user.id).first()
    if not es:
        es = EmailSettings(user_id=user.id)
        db.add(es)

    es.provider = "smtp"
    es.smtp_host = smtp_host.strip()
    es.smtp_port = smtp_port
    es.smtp_username = smtp_username.strip()
    es.smtp_password_encrypted = encrypt(smtp_password)
    es.smtp_from_email = smtp_from_email.strip()
    es.smtp_use_tls = smtp_use_tls
    db.commit()
    return HTMLResponse('<span class="saved-flash">SMTP configured</span>')


# --- Email Provider: SendGrid ---

@router.post("/api/settings/email/sendgrid")
def save_sendgrid(
    request: Request,
    sendgrid_api_key: str = Form(...),
    sendgrid_from_email: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    es = db.query(EmailSettings).filter_by(user_id=user.id).first()
    if not es:
        es = EmailSettings(user_id=user.id)
        db.add(es)

    es.provider = "sendgrid"
    es.sendgrid_api_key_encrypted = encrypt(sendgrid_api_key.strip())
    es.sendgrid_from_email = sendgrid_from_email.strip()
    db.commit()
    return HTMLResponse('<span class="saved-flash">SendGrid configured</span>')


# --- Email Provider: Gmail OAuth ---

@router.get("/api/settings/email/gmail/url")
def gmail_oauth_url(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    s = get_settings()
    if not s.gmail_client_id:
        return HTMLResponse('<div class="error-msg">Gmail OAuth not configured on server</div>')

    base_url = str(request.base_url).rstrip("/")
    params = {
        "client_id": s.gmail_client_id,
        "redirect_uri": f"{base_url}/api/settings/email/gmail/callback",
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/userinfo.email",
        "access_type": "offline",
        "prompt": "consent",
        "state": str(user.id),
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(url)


@router.get("/api/settings/email/gmail/callback")
def gmail_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    s = get_settings()
    base_url = str(request.base_url).rstrip("/")

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": s.gmail_client_id,
        "client_secret": s.gmail_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": f"{base_url}/api/settings/email/gmail/callback",
    })
    if resp.status_code != 200:
        return RedirectResponse("/settings?error=gmail_failed")

    data = resp.json()
    user_id = int(state)

    # Get email address
    info_resp = requests.get("https://www.googleapis.com/oauth2/v2/userinfo",
                             headers={"Authorization": f"Bearer {data['access_token']}"})
    gmail_email = info_resp.json().get("email", "") if info_resp.status_code == 200 else ""

    es = db.query(EmailSettings).filter_by(user_id=user_id).first()
    if not es:
        es = EmailSettings(user_id=user_id)
        db.add(es)

    es.provider = "gmail"
    es.gmail_email_address = gmail_email
    es.gmail_access_token = encrypt(data["access_token"])
    es.gmail_refresh_token = encrypt(data.get("refresh_token", ""))
    es.gmail_token_expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    db.commit()

    return RedirectResponse("/settings?success=gmail")


# --- Email Provider: Outlook OAuth ---

@router.get("/api/settings/email/outlook/url")
def outlook_oauth_url(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    s = get_settings()
    if not s.outlook_client_id:
        return HTMLResponse('<div class="error-msg">Outlook OAuth not configured on server</div>')

    base_url = str(request.base_url).rstrip("/")
    params = {
        "client_id": s.outlook_client_id,
        "redirect_uri": f"{base_url}/api/settings/email/outlook/callback",
        "response_type": "code",
        "scope": "offline_access Mail.Send User.Read",
        "state": str(user.id),
    }
    url = f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{urlencode(params)}"
    return RedirectResponse(url)


@router.get("/api/settings/email/outlook/callback")
def outlook_callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    s = get_settings()
    base_url = str(request.base_url).rstrip("/")

    resp = requests.post("https://login.microsoftonline.com/common/oauth2/v2.0/token", data={
        "client_id": s.outlook_client_id,
        "client_secret": s.outlook_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": f"{base_url}/api/settings/email/outlook/callback",
        "scope": "offline_access Mail.Send User.Read",
    })
    if resp.status_code != 200:
        return RedirectResponse("/settings?error=outlook_failed")

    data = resp.json()
    user_id = int(state)

    # Get email
    info_resp = requests.get("https://graph.microsoft.com/v1.0/me",
                             headers={"Authorization": f"Bearer {data['access_token']}"})
    outlook_email = info_resp.json().get("mail", "") if info_resp.status_code == 200 else ""

    es = db.query(EmailSettings).filter_by(user_id=user_id).first()
    if not es:
        es = EmailSettings(user_id=user_id)
        db.add(es)

    es.provider = "outlook"
    es.outlook_email_address = outlook_email
    es.outlook_access_token = encrypt(data["access_token"])
    es.outlook_refresh_token = encrypt(data.get("refresh_token", ""))
    es.outlook_token_expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    db.commit()

    return RedirectResponse("/settings?success=outlook")


# --- Disconnect / Test ---

@router.delete("/api/settings/email/disconnect")
def disconnect_email(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    es = db.query(EmailSettings).filter_by(user_id=user.id).first()
    if es:
        es.provider = "none"
        es.gmail_access_token = None
        es.gmail_refresh_token = None
        es.gmail_email_address = None
        es.outlook_access_token = None
        es.outlook_refresh_token = None
        es.outlook_email_address = None
        es.smtp_host = None
        es.smtp_password_encrypted = None
        es.sendgrid_api_key_encrypted = None
        db.commit()
    return HTMLResponse('<span class="saved-flash">Email disconnected</span>')


@router.post("/api/settings/email/test")
def test_email(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    try:
        from app.services.email_senders import send_email_for_user
        result = send_email_for_user(
            db, user.id, user.email,
            "LeadBlitz Test Email",
            "<h2>Test email from LeadBlitz</h2><p>Your email provider is configured correctly.</p>",
        )
        return HTMLResponse('<span class="saved-flash">Test email sent</span>')
    except Exception as e:
        return HTMLResponse(f'<div class="error-msg">{e}</div>')
