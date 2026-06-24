"""
Outbound email for the Cedar Ridge Inbox Agent. Everything the agent needs to tell
the user — high-priority alerts, stage-change confirmations, the daily radar digest,
replies to direct notes — goes out as an email from agent@cedarridge.capital to
OWNER_EMAIL, rather than Telegram. Reuses the IMAP mailbox's own credentials for SMTP
unless separate ones are configured.
"""
from __future__ import annotations
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("CRM_SMTP_HOST") or os.getenv("CRM_IMAP_HOST")
SMTP_PORT = int(os.getenv("CRM_SMTP_PORT", 587))
SMTP_USER = os.getenv("CRM_SMTP_USER") or os.getenv("CRM_IMAP_USER")
SMTP_PASSWORD = os.getenv("CRM_SMTP_PASSWORD") or os.getenv("CRM_IMAP_PASSWORD")
OWNER_EMAIL = os.getenv("OWNER_EMAIL")


def send(subject: str, body_html: str, in_reply_to: str | None = None):
    """Synchronous SMTP send — call via asyncio.to_thread from async code."""
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and OWNER_EMAIL):
        logger.warning("crm_mail: SMTP not configured, skipping send")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = OWNER_EMAIL
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    # Callers write plain "\n" for line breaks; convert so it actually renders in email clients.
    msg.attach(MIMEText(body_html.replace("\n", "<br>\n"), "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [OWNER_EMAIL], msg.as_string())
    except Exception as e:
        logger.error(f"crm_mail: send failed: {e}")


async def send_async(subject: str, body_html: str, in_reply_to: str | None = None):
    import asyncio
    await asyncio.to_thread(send, subject, body_html, in_reply_to)
