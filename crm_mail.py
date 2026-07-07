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
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("CRM_SMTP_HOST") or os.getenv("CRM_IMAP_HOST")
SMTP_PORT = int(os.getenv("CRM_SMTP_PORT", 587))
SMTP_USER = os.getenv("CRM_SMTP_USER") or os.getenv("CRM_IMAP_USER")
SMTP_PASSWORD = os.getenv("CRM_SMTP_PASSWORD") or os.getenv("CRM_IMAP_PASSWORD")
# Comma-separated to support multiple owners (e.g. "joe@x.com, sanjhay@y.com"). Replies go back
# to whoever emailed (see `to`); broadcasts (digests) default to the first/primary owner.
OWNER_EMAILS = [e.strip() for e in (os.getenv("OWNER_EMAIL") or "").split(",") if e.strip()]
PRIMARY_OWNER = OWNER_EMAILS[0] if OWNER_EMAILS else None


def is_owner(addr: str | None) -> bool:
    return bool(addr) and addr.strip().lower() in {o.lower() for o in OWNER_EMAILS}


def send(
    subject: str, body_html: str, in_reply_to: str | None = None,
    attachment: tuple[str, bytes] | None = None, to: str | None = None,
) -> str | None:
    """Synchronous SMTP send — call via asyncio.to_thread from async code.
    attachment, if given, is (filename, content_bytes) — e.g. a generated brief PDF.
    Returns the Message-ID used for this send (None if the send was skipped/failed),
    so callers that need to correlate a later reply back to this specific email
    (e.g. duplicate-name flags) can record it."""
    # Reply to the owner who emailed; anything else (or a non-owner address) → primary owner.
    # This preserves the safety property that the agent only ever emails owners, never prospects.
    recipient = to if is_owner(to) else PRIMARY_OWNER
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and recipient):
        logger.warning("crm_mail: SMTP not configured, skipping send")
        return None

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = recipient
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    # Callers write plain "\n" for line breaks; convert so it actually renders in email clients.
    msg.attach(MIMEText(body_html.replace("\n", "<br>\n"), "html"))

    if attachment:
        filename, content = attachment
        part = MIMEApplication(content, Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [recipient], msg.as_string())
        import crm_store
        crm_store.log_event("reply_sent", {"to": recipient, "subject": subject})
        return message_id
    except Exception as e:
        logger.error(f"crm_mail: send failed: {e}")
        import crm_store
        crm_store.log_event("reply_failed", {"subject": subject, "error": str(e)})
        return None


async def send_async(
    subject: str, body_html: str, in_reply_to: str | None = None,
    attachment: tuple[str, bytes] | None = None, to: str | None = None,
) -> str | None:
    import asyncio
    return await asyncio.to_thread(send, subject, body_html, in_reply_to, attachment, to)
