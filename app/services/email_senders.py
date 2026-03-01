import base64
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmailSettings as EmailSettingsModel
from app.services.encryption import encrypt, decrypt

logger = logging.getLogger(__name__)


class EmailProviderError(Exception):
    pass


def get_email_settings(db: Session, user_id: int) -> Optional[EmailSettingsModel]:
    return db.query(EmailSettingsModel).filter(EmailSettingsModel.user_id == user_id).first()


def refresh_gmail_token(settings: EmailSettingsModel, db: Session) -> str:
    if not settings.gmail_refresh_token:
        raise EmailProviderError("No Gmail refresh token available")

    refresh_token = decrypt(settings.gmail_refresh_token)
    s = get_settings()

    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": s.gmail_client_id,
        "client_secret": s.gmail_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    if resp.status_code != 200:
        raise EmailProviderError(f"Failed to refresh Gmail token: {resp.text}")

    data = resp.json()
    settings.gmail_access_token = encrypt(data["access_token"])
    settings.gmail_token_expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    db.commit()
    return decrypt(settings.gmail_access_token)


def send_via_gmail(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str, db: Session) -> dict:
    if not settings.gmail_access_token or not settings.gmail_email_address:
        raise EmailProviderError("Gmail not properly configured")

    if settings.gmail_token_expiry and datetime.utcnow() >= settings.gmail_token_expiry:
        access_token = refresh_gmail_token(settings, db)
    else:
        access_token = decrypt(settings.gmail_access_token)

    message = MIMEMultipart("alternative")
    message["From"] = settings.gmail_email_address
    message["To"] = to_email
    message["Subject"] = subject
    message.attach(MIMEText(html_body, "html"))

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"raw": raw_message},
    )
    if resp.status_code != 200:
        raise EmailProviderError(f"Gmail API error: {resp.text}")
    return {"success": True, "provider": "gmail", "message_id": resp.json().get("id")}


def refresh_outlook_token(settings: EmailSettingsModel, db: Session) -> str:
    if not settings.outlook_refresh_token:
        raise EmailProviderError("No Outlook refresh token available")

    refresh_token = decrypt(settings.outlook_refresh_token)
    s = get_settings()

    resp = requests.post("https://login.microsoftonline.com/common/oauth2/v2.0/token", data={
        "client_id": s.outlook_client_id,
        "client_secret": s.outlook_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        "scope": "offline_access Mail.Send User.Read",
    })
    if resp.status_code != 200:
        raise EmailProviderError(f"Failed to refresh Outlook token: {resp.text}")

    data = resp.json()
    settings.outlook_access_token = encrypt(data["access_token"])
    settings.outlook_token_expiry = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 3600))
    db.commit()
    return decrypt(settings.outlook_access_token)


def send_via_outlook(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str, db: Session) -> dict:
    if not settings.outlook_access_token or not settings.outlook_email_address:
        raise EmailProviderError("Outlook not properly configured")

    if settings.outlook_token_expiry and datetime.utcnow() >= settings.outlook_token_expiry:
        access_token = refresh_outlook_token(settings, db)
    else:
        access_token = decrypt(settings.outlook_access_token)

    resp = requests.post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to_email}}],
        }},
    )
    if resp.status_code != 202:
        raise EmailProviderError(f"Outlook Graph API error: {resp.text}")
    return {"success": True, "provider": "outlook"}


def send_via_smtp(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str) -> dict:
    if not all([settings.smtp_host, settings.smtp_port, settings.smtp_username,
                settings.smtp_password_encrypted, settings.smtp_from_email]):
        raise EmailProviderError("SMTP not properly configured")

    smtp_password = decrypt(settings.smtp_password_encrypted)
    message = MIMEMultipart("alternative")
    message["From"] = settings.smtp_from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.attach(MIMEText(html_body, "html"))

    import socket as _socket
    old_timeout = _socket.getdefaulttimeout()
    _socket.setdefaulttimeout(5)
    try:
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=5)
        server.login(settings.smtp_username, smtp_password)
        server.send_message(message)
        server.quit()
        return {"success": True, "provider": "smtp"}
    except (TimeoutError, _socket.timeout, OSError) as e:
        raise EmailProviderError(f"SMTP connection timed out: {e}")
    except Exception as e:
        raise EmailProviderError(f"SMTP error: {e}")
    finally:
        _socket.setdefaulttimeout(old_timeout)


def send_via_sendgrid(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str) -> dict:
    if not settings.sendgrid_api_key_encrypted or not settings.sendgrid_from_email:
        raise EmailProviderError("SendGrid not properly configured")

    api_key = decrypt(settings.sendgrid_api_key_encrypted)
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": settings.sendgrid_from_email},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        },
    )
    if resp.status_code not in [200, 202]:
        raise EmailProviderError(f"SendGrid API error: {resp.text}")
    return {"success": True, "provider": "sendgrid"}


def send_email_for_user(db: Session, user_id: int, to_email: str, subject: str, html_body: str) -> dict:
    settings = get_email_settings(db, user_id)
    if not settings or settings.provider == "none":
        raise EmailProviderError("No email provider configured. Please set up an email provider in Settings.")

    if settings.provider == "gmail":
        return send_via_gmail(settings, to_email, subject, html_body, db)
    elif settings.provider == "outlook":
        return send_via_outlook(settings, to_email, subject, html_body, db)
    elif settings.provider == "smtp":
        return send_via_smtp(settings, to_email, subject, html_body)
    elif settings.provider == "sendgrid":
        return send_via_sendgrid(settings, to_email, subject, html_body)
    raise EmailProviderError(f"Unsupported email provider: {settings.provider}")


def _build_mime_with_attachment(
    from_email: str, to_email: str, subject: str, html_body: str,
    attachments: list[tuple[bytes, str, str]],
) -> MIMEMultipart:
    """Build a MIME message with one or more attachments.

    Each attachment is a (bytes, filename, mime_type) tuple.
    """
    message = MIMEMultipart("mixed")
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.attach(MIMEText(html_body, "html"))
    for att_bytes, att_filename, att_mime in attachments:
        _maintype, subtype = att_mime.split("/", 1)
        part = MIMEApplication(att_bytes, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=att_filename)
        message.attach(part)
    return message


def send_email_with_attachments_for_user(
    db: Session, user_id: int, to_email: str, subject: str, html_body: str,
    attachments: list[tuple[bytes, str, str]],
) -> dict:
    """Send an email with one or more attachments.

    Each entry in *attachments* is a (bytes, filename, mime_type) tuple.
    """
    settings = get_email_settings(db, user_id)
    if not settings or settings.provider == "none":
        raise EmailProviderError("No email provider configured.")

    if settings.provider == "gmail":
        if settings.gmail_token_expiry and datetime.utcnow() >= settings.gmail_token_expiry:
            access_token = refresh_gmail_token(settings, db)
        else:
            access_token = decrypt(settings.gmail_access_token)
        message = _build_mime_with_attachment(
            settings.gmail_email_address, to_email, subject, html_body, attachments)
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        resp = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"raw": raw_message})
        if resp.status_code != 200:
            raise EmailProviderError(f"Gmail API error: {resp.text}")
        return {"success": True, "provider": "gmail"}

    elif settings.provider == "outlook":
        if settings.outlook_token_expiry and datetime.utcnow() >= settings.outlook_token_expiry:
            access_token = refresh_outlook_token(settings, db)
        else:
            access_token = decrypt(settings.outlook_access_token)
        outlook_attachments = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att_filename,
                "contentType": att_mime,
                "contentBytes": base64.b64encode(att_bytes).decode(),
            }
            for att_bytes, att_filename, att_mime in attachments
        ]
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": to_email}}],
                "attachments": outlook_attachments,
            }})
        if resp.status_code != 202:
            raise EmailProviderError(f"Outlook error: {resp.text}")
        return {"success": True, "provider": "outlook"}

    elif settings.provider == "smtp":
        smtp_password = decrypt(settings.smtp_password_encrypted)
        message = _build_mime_with_attachment(
            settings.smtp_from_email, to_email, subject, html_body, attachments)
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=5)
        server.login(settings.smtp_username, smtp_password)
        server.send_message(message)
        server.quit()
        return {"success": True, "provider": "smtp"}

    elif settings.provider == "sendgrid":
        api_key = decrypt(settings.sendgrid_api_key_encrypted)
        sg_attachments = [
            {
                "content": base64.b64encode(att_bytes).decode(),
                "type": att_mime,
                "filename": att_filename,
                "disposition": "attachment",
            }
            for att_bytes, att_filename, att_mime in attachments
        ]
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": settings.sendgrid_from_email},
                "subject": subject,
                "content": [{"type": "text/html", "value": html_body}],
                "attachments": sg_attachments,
            })
        if resp.status_code not in [200, 202]:
            raise EmailProviderError(f"SendGrid error: {resp.text}")
        return {"success": True, "provider": "sendgrid"}

    raise EmailProviderError(f"Unsupported provider: {settings.provider}")


def send_email_with_attachment_for_user(
    db: Session, user_id: int, to_email: str, subject: str, html_body: str,
    attachment_bytes: bytes, attachment_filename: str, attachment_mime: str = "application/pdf",
) -> dict:
    """Backward-compatible wrapper — sends a single attachment."""
    return send_email_with_attachments_for_user(
        db, user_id, to_email, subject, html_body,
        [(attachment_bytes, attachment_filename, attachment_mime)],
    )
