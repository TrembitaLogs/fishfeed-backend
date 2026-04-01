"""Email service for sending transactional emails via SMTP."""

import logging
from email.message import EmailMessage

import aiosmtplib

from app.config import get_settings

logger = logging.getLogger(__name__)


async def send_email(to: str, subject: str, body_html: str) -> bool:
    """Send an email via SMTP.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body_html: HTML body content.

    Returns:
        True if sent successfully, False otherwise.
    """
    settings = get_settings()

    if not settings.SMTP_HOST:
        logger.warning("SMTP not configured, skipping email to %s", to)
        return False

    message = EmailMessage()
    message["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body_html, subtype="html")

    try:
        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME,
            password=settings.SMTP_PASSWORD,
            start_tls=settings.SMTP_USE_TLS,
        )
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False


async def send_password_reset_email(to: str, reset_token: str) -> bool:
    """Send a password reset email with a reset link.

    Args:
        to: Recipient email address.
        reset_token: The password reset token.

    Returns:
        True if sent successfully, False otherwise.
    """
    settings = get_settings()
    reset_url = f"{settings.PASSWORD_RESET_BASE_URL}?token={reset_token}"
    expire_minutes = settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES

    subject = "Reset your FishFeed password"
    body_html = f"""\
<html>
<body>
<h2>Password Reset</h2>
<p>You requested a password reset for your FishFeed account.</p>
<p>Click the link below to reset your password.
This link expires in {expire_minutes} minutes.</p>
<p><a href="{reset_url}">Reset Password</a></p>
<p>If you didn't request this, you can safely ignore this email.</p>
</body>
</html>"""

    return await send_email(to, subject, body_html)
