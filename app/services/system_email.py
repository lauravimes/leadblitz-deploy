import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.config import get_settings

logger = logging.getLogger(__name__)


def is_smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_user and s.smtp_password and s.smtp_host)


def send_system_email(to_email: str, subject: str, html_body: str) -> bool:
    s = get_settings()
    if not is_smtp_configured():
        logger.warning("[SYSTEM EMAIL] SMTP not configured — set SMTP_HOST/USER/PASSWORD env vars")
        return False

    from_email = s.smtp_from_email or f"noreply@{s.smtp_host}"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(s.smtp_user, s.smtp_password)
            server.sendmail(from_email, to_email, msg.as_string())

        logger.info(f"[SYSTEM EMAIL] Sent '{subject}' to {to_email}")
        return True
    except Exception as e:
        logger.error(f"[SYSTEM EMAIL] Failed to send to {to_email}: {e}")
        return False


def build_branded_email(
    heading: str,
    body_content: str,
    button_text: str = None,
    button_url: str = None,
    footer_note: str = None,
) -> str:
    button_html = ""
    if button_text and button_url:
        button_html = f"""
            <div style="text-align: center; margin: 32px 0;">
                <a href="{button_url}"
                   style="display: inline-block; background: #111; color: #fff;
                          text-decoration: none; padding: 14px 40px; border-radius: 6px;
                          font-weight: 600; font-size: 16px;">
                    {button_text}
                </a>
            </div>
            <p style="text-align: center; font-size: 12px; color: #9ca3af; margin-top: 8px;">
                Or copy this link: <a href="{button_url}" style="color: #0066ff; word-break: break-all;">{button_url}</a>
            </p>
        """

    footer_html = ""
    if footer_note:
        footer_html = f'<p style="color: #6b7280; font-size: 14px; margin-top: 24px;">{footer_note}</p>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; background-color: #fafafa; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <div style="max-width: 600px; margin: 0 auto; padding: 40px 20px;">
        <div style="background: #111; padding: 24px; text-align: center;">
            <h1 style="color: #fff; margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.5px;">
                LeadBlitz
            </h1>
        </div>
        <div style="background: #fff; padding: 40px 32px; border: 1px solid #e0e0e0; border-top: none;">
            <h2 style="color: #111; margin: 0 0 20px; font-size: 20px; font-weight: 600;">{heading}</h2>
            <div style="color: #374151; font-size: 15px; line-height: 1.7;">{body_content}</div>
            {button_html}
            {footer_html}
        </div>
        <div style="text-align: center; padding: 24px 0; color: #9ca3af; font-size: 12px;">
            <p style="margin: 0;">&copy; LeadBlitz. All rights reserved.</p>
        </div>
    </div>
</body>
</html>"""
