"""Outgoing email for user-configured providers (SMTP, SendGrid, Gmail, Outlook).

Design:

* ``deliver_email(settings, ...)`` does the network work and needs **no DB
  session** — callers load ``EmailSettings`` first, close their session, then
  deliver. The background send worker relies on this so a slow SMTP server
  never pins a Postgres connection.
* Every provider failure surfaces as ``EmailProviderError`` with a message a
  user can act on; nothing else escapes.
* Bodies are sent as ``multipart/alternative`` (text/plain + text/html) and the
  user's saved signature is appended by ``prepare_body``.
"""
import base64
import html as _html
import logging
import re
import smtplib
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterator, Optional, Sequence, Tuple

import requests
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import EmailSettings as EmailSettingsModel, EmailSignature
from app.services.encryption import decrypt, encrypt

logger = logging.getLogger(__name__)

Attachment = Tuple[bytes, str, str]  # (bytes, filename, mime_type)

_HTTP_TIMEOUT = 30
_SMTP_TIMEOUT = 20


class EmailProviderError(Exception):
    pass


def get_email_settings(db: Session, user_id: int) -> Optional[EmailSettingsModel]:
    return db.query(EmailSettingsModel).filter(EmailSettingsModel.user_id == user_id).first()


# --- Body helpers -----------------------------------------------------------

_BLOCK_HTML = re.compile(r"<(p|br|div|table|ul|ol|h[1-6])\b", re.IGNORECASE)


def _nl2br(text: str) -> str:
    """Turn plain-text newlines into ``<br>``.

    Bodies that already contain block-level HTML (``<p>``, ``<br>``, ``<div>``…)
    are returned untouched — adding ``<br>`` to them double-spaces every line.
    """
    if not text:
        return ""
    if _BLOCK_HTML.search(text):
        return text
    return text.replace("\r\n", "\n").replace("\n", "<br>\n")


def html_to_text(html_body: str) -> str:
    """Rough text/plain rendering of an HTML body for the alternative part."""
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", html_body or "")
    text = re.sub(r"(?i)</\s*(p|div|h[1-6]|li|tr)\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def signature_html(sig: Optional[EmailSignature]) -> str:
    """Render the saved signature as a small HTML block ("" when nothing is set)."""
    if not sig:
        return ""
    lines = []
    if sig.full_name:
        lines.append(f"<strong>{_html.escape(sig.full_name)}</strong>")
    title = " · ".join(_html.escape(x) for x in (sig.position or "", sig.company_name or "") if x)
    if title:
        lines.append(title)
    if sig.phone:
        lines.append(_html.escape(sig.phone))
    if sig.website:
        href = sig.website if sig.website.startswith(("http://", "https://")) else f"https://{sig.website}"
        lines.append(f'<a href="{_html.escape(href, quote=True)}">{_html.escape(sig.website)}</a>')
    if not lines:
        return ""
    return '<div class="signature" style="margin-top:16px">' + "<br>\n".join(lines) + "</div>"


def prepare_body(html_body: str, sig: Optional[EmailSignature] = None) -> str:
    """Normalise a user-typed body and append the saved signature."""
    body = _nl2br(html_body)
    signature = signature_html(sig)
    if signature:
        body = f"{body}\n{signature}"
    return body


def build_message(
    from_email: str, to_email: str, subject: str, html_body: str,
    attachments: Optional[Sequence[Attachment]] = None,
) -> MIMEMultipart:
    """text/plain + text/html alternative, wrapped in multipart/mixed when there
    are attachments. Attachment MIME types keep their real maintype."""
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(html_to_text(html_body), "plain", "utf-8"))
    alternative.attach(MIMEText(html_body, "html", "utf-8"))

    if attachments:
        message = MIMEMultipart("mixed")
        message.attach(alternative)
        for att_bytes, att_filename, att_mime in attachments:
            maintype, _, subtype = (att_mime or "application/octet-stream").partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(att_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=att_filename)
            message.attach(part)
    else:
        message = alternative

    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject
    return message


# --- Error translation --------------------------------------------------------

def _friendly_error(provider: str, exc: BaseException, settings: EmailSettingsModel) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("SMTP authentication failed — check the username and password "
                "(Gmail and Outlook need an App Password, not your login password).")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"The mail server refused the recipient: {exc.recipients}"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "The SMTP server closed the connection unexpectedly — check the port/TLS setting."
    if isinstance(exc, smtplib.SMTPException):
        return f"SMTP error: {exc}"
    if isinstance(exc, (socket.timeout, TimeoutError, requests.Timeout)):
        host = f"{settings.smtp_host}:{settings.smtp_port}" if provider == "smtp" else provider
        return f"Connection to {host} timed out. Check the host, port and TLS settings."
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror)):
        return f"Could not connect to {settings.smtp_host}:{settings.smtp_port} — check the SMTP host and port."
    if isinstance(exc, requests.RequestException):
        return f"Network error talking to {provider}: {exc}"
    if isinstance(exc, OSError):
        return f"Network error: {exc}"
    return f"{provider} error: {exc}"


@contextmanager
def _provider_errors(provider: str, settings: EmailSettingsModel) -> Iterator[None]:
    try:
        yield
    except EmailProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 — everything becomes a user-facing error
        logger.warning("[email] %s send failed: %s", provider, exc)
        raise EmailProviderError(_friendly_error(provider, exc, settings)) from exc


# --- OAuth token refresh (Gmail / Outlook) ------------------------------------

def _persist_tokens(settings: EmailSettingsModel, **fields) -> None:
    """Write refreshed tokens with a short-lived session so callers can stay
    session-free across the network call."""
    from app.database import SessionLocal

    for key, value in fields.items():
        setattr(settings, key, value)
    with SessionLocal() as db:
        db.query(EmailSettingsModel).filter(EmailSettingsModel.id == settings.id).update(fields)
        db.commit()


def _token_expired(expiry: Optional[datetime]) -> bool:
    if not expiry:
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expiry


def refresh_gmail_token(settings: EmailSettingsModel) -> str:
    if not settings.gmail_refresh_token:
        raise EmailProviderError("Gmail access has expired — reconnect Gmail in Settings.")
    s = get_settings()
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": s.gmail_client_id,
        "client_secret": s.gmail_client_secret,
        "refresh_token": decrypt(settings.gmail_refresh_token),
        "grant_type": "refresh_token",
    }, timeout=_HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise EmailProviderError(f"Failed to refresh Gmail token: {resp.text}")
    data = resp.json()
    _persist_tokens(
        settings,
        gmail_access_token=encrypt(data["access_token"]),
        gmail_token_expiry=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
    )
    return data["access_token"]


def refresh_outlook_token(settings: EmailSettingsModel) -> str:
    if not settings.outlook_refresh_token:
        raise EmailProviderError("Outlook access has expired — reconnect Outlook in Settings.")
    s = get_settings()
    resp = requests.post("https://login.microsoftonline.com/common/oauth2/v2.0/token", data={
        "client_id": s.outlook_client_id,
        "client_secret": s.outlook_client_secret,
        "refresh_token": decrypt(settings.outlook_refresh_token),
        "grant_type": "refresh_token",
        "scope": "offline_access Mail.Send User.Read",
    }, timeout=_HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise EmailProviderError(f"Failed to refresh Outlook token: {resp.text}")
    data = resp.json()
    _persist_tokens(
        settings,
        outlook_access_token=encrypt(data["access_token"]),
        outlook_token_expiry=datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 3600)),
    )
    return data["access_token"]


# --- Providers ------------------------------------------------------------------

def send_via_gmail(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str,
                   attachments: Optional[Sequence[Attachment]] = None) -> dict:
    if not settings.gmail_access_token or not settings.gmail_email_address:
        raise EmailProviderError("Gmail is not connected — connect it in Settings.")
    access_token = (refresh_gmail_token(settings) if _token_expired(settings.gmail_token_expiry)
                    else decrypt(settings.gmail_access_token))
    message = build_message(settings.gmail_email_address, to_email, subject, html_body, attachments)
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()},
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise EmailProviderError(f"Gmail API error: {resp.text}")
    return {"success": True, "provider": "gmail", "message_id": resp.json().get("id")}


def send_via_outlook(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str,
                     attachments: Optional[Sequence[Attachment]] = None) -> dict:
    if not settings.outlook_access_token or not settings.outlook_email_address:
        raise EmailProviderError("Outlook is not connected — connect it in Settings.")
    access_token = (refresh_outlook_token(settings) if _token_expired(settings.outlook_token_expiry)
                    else decrypt(settings.outlook_access_token))
    payload = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }
    if attachments:
        payload["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name, "contentType": mime,
                "contentBytes": base64.b64encode(data).decode(),
            }
            for data, name, mime in attachments
        ]
    resp = requests.post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"message": payload},
        timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code != 202:
        raise EmailProviderError(f"Outlook Graph API error: {resp.text}")
    return {"success": True, "provider": "outlook"}


def send_via_smtp(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str,
                  attachments: Optional[Sequence[Attachment]] = None) -> dict:
    if not all([settings.smtp_host, settings.smtp_port, settings.smtp_username,
                settings.smtp_password_encrypted, settings.smtp_from_email]):
        raise EmailProviderError("SMTP is not fully configured — fill in host, port, username, password and from-address in Settings.")
    password = decrypt(settings.smtp_password_encrypted)
    if not password:
        raise EmailProviderError("Stored SMTP password could not be read — re-enter it in Settings.")

    message = build_message(settings.smtp_from_email, to_email, subject, html_body, attachments)
    if settings.smtp_use_tls:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT)
        server.starttls()
    else:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT)
    try:
        server.login(settings.smtp_username, password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — connection may already be gone
            pass
    return {"success": True, "provider": "smtp"}


def send_via_sendgrid(settings: EmailSettingsModel, to_email: str, subject: str, html_body: str,
                      attachments: Optional[Sequence[Attachment]] = None) -> dict:
    if not settings.sendgrid_api_key_encrypted or not settings.sendgrid_from_email:
        raise EmailProviderError("SendGrid is not configured — add the API key and from-address in Settings.")
    api_key = decrypt(settings.sendgrid_api_key_encrypted)
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": settings.sendgrid_from_email},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": html_to_text(html_body) or " "},
            {"type": "text/html", "value": html_body},
        ],
    }
    if attachments:
        payload["attachments"] = [
            {"content": base64.b64encode(data).decode(), "type": mime, "filename": name, "disposition": "attachment"}
            for data, name, mime in attachments
        ]
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload, timeout=_HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 202):
        raise EmailProviderError(f"SendGrid API error: {resp.text}")
    return {"success": True, "provider": "sendgrid"}


_PROVIDERS = {
    "gmail": send_via_gmail,
    "outlook": send_via_outlook,
    "smtp": send_via_smtp,
    "sendgrid": send_via_sendgrid,
}


def deliver_email(
    settings: Optional[EmailSettingsModel], to_email: str, subject: str, html_body: str,
    attachments: Optional[Sequence[Attachment]] = None,
) -> dict:
    """Send through the user's configured provider. Needs no DB session.

    ``html_body`` should already have gone through ``prepare_body``.
    Raises ``EmailProviderError`` for every failure mode.
    """
    if not settings or settings.provider == "none":
        raise EmailProviderError("No email provider configured. Set one up in Settings → Email.")
    sender = _PROVIDERS.get(settings.provider)
    if sender is None:
        raise EmailProviderError(f"Unsupported email provider: {settings.provider}")
    if not to_email or "@" not in to_email:
        raise EmailProviderError(f"Invalid recipient address: {to_email!r}")
    with _provider_errors(settings.provider, settings):
        return sender(settings, to_email, subject, html_body, attachments)


def send_email_for_user(
    db: Session, user_id: int, to_email: str, subject: str, html_body: str,
    attachments: Optional[Sequence[Attachment]] = None,
) -> dict:
    """Convenience wrapper for request handlers: load settings + signature, then deliver."""
    settings = get_email_settings(db, user_id)
    sig = db.query(EmailSignature).filter_by(user_id=user_id).first()
    return deliver_email(settings, to_email, subject, prepare_body(html_body, sig), attachments)
