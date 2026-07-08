"""
Outbound email for the Cedar Ridge Inbox Agent. Everything the agent needs to tell
the user — high-priority alerts, stage-change confirmations, the daily radar digest,
replies to direct notes — goes out as an email from agent@cedarridge.capital to
OWNER_EMAIL, rather than Telegram.

Two transports:
  * Resend (HTTPS, port 443) — the PRODUCTION path. Railway (and most cloud hosts)
    block outbound SMTP (ports 25/465/587), so a raw smtplib send just hangs and times
    out. Resend sends over ordinary HTTPS, which is never blocked. Enabled by setting
    RESEND_API_KEY; the sending domain must be verified in Resend to send as
    agent@cedarridge.capital.
  * SMTP (smtplib) — LOCAL-DEV fallback, used only when RESEND_API_KEY is unset. Reuses
    the IMAP mailbox's own credentials so it works off the existing CRM_IMAP_* vars.

Receiving (IMAP, crm_mailbox.py) is unaffected — this module is send-only.
"""
from __future__ import annotations
import base64
import logging
import os
import smtplib
import socket
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

import httpx

logger = logging.getLogger(__name__)

# Resend (HTTPS) — production transport. FROM must be on a domain verified in Resend.
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_ENDPOINT = "https://api.resend.com/emails"

# SMTP — local-dev fallback only. Derive the SMTP host from the IMAP host
# (imap.gmail.com -> smtp.gmail.com) so it works off the existing CRM_IMAP_* vars.
SMTP_HOST = os.getenv("CRM_SMTP_HOST") or (os.getenv("CRM_IMAP_HOST") or "").replace("imap.", "smtp.")
SMTP_PORT = int(os.getenv("CRM_SMTP_PORT", 587))
SMTP_USER = os.getenv("CRM_SMTP_USER") or os.getenv("CRM_IMAP_USER")
SMTP_PASSWORD = os.getenv("CRM_SMTP_PASSWORD") or os.getenv("CRM_IMAP_PASSWORD")

# The address the agent sends FROM. Defaults to the mailbox login (agent@cedarridge.capital).
# Set CRM_FROM_EMAIL to override, e.g. "Cedar Ridge CRM <agent@cedarridge.capital>". Until the
# domain is verified in Resend you can set this to "onboarding@resend.dev" for an interim test.
FROM_EMAIL = os.getenv("CRM_FROM_EMAIL") or SMTP_USER
# Where replies should go. Always the agent inbox so the owner's reply comes back to be
# processed — matters when FROM is a Resend test sender (onboarding@resend.dev) rather than
# the real mailbox. Defaults to the mailbox login (agent@cedarridge.capital).
REPLY_TO = os.getenv("CRM_REPLY_TO") or SMTP_USER

# Comma-separated to support multiple owners (e.g. "joe@x.com, sanjhay@y.com"). Replies go back
# to whoever emailed (see `to`); broadcasts (digests) default to the first/primary owner.
OWNER_EMAILS = [e.strip() for e in (os.getenv("OWNER_EMAIL") or "").split(",") if e.strip()]
PRIMARY_OWNER = OWNER_EMAILS[0] if OWNER_EMAILS else None


def is_owner(addr: str | None) -> bool:
    return bool(addr) and addr.strip().lower() in {o.lower() for o in OWNER_EMAILS}


def _send_via_resend(
    subject: str, html: str, recipient: str, message_id: str,
    in_reply_to: str | None, attachment: tuple[str, bytes] | None,
) -> None:
    """POST the email to Resend over HTTPS. Raises on any non-2xx so the caller logs it."""
    payload: dict = {
        "from": FROM_EMAIL,
        "to": [recipient],
        "subject": subject,
        "html": html,
        # Set our own Message-ID so a later reply's In-Reply-To can be correlated back
        # (duplicate-name flags), and thread the reply into the original conversation.
        "headers": {"Message-ID": message_id},
    }
    if REPLY_TO and REPLY_TO != FROM_EMAIL:
        payload["reply_to"] = REPLY_TO
    if in_reply_to:
        payload["headers"]["In-Reply-To"] = in_reply_to
        payload["headers"]["References"] = in_reply_to
    if attachment:
        filename, content = attachment
        payload["attachments"] = [{"filename": filename, "content": base64.b64encode(content).decode()}]

    r = httpx.post(
        RESEND_ENDPOINT, json=payload,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"}, timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Resend {r.status_code}: {r.text[:300]}")


def _send_via_smtp(
    subject: str, html: str, recipient: str, message_id: str,
    in_reply_to: str | None, attachment: tuple[str, bytes] | None,
) -> None:
    """Local-dev fallback. Forces IPv4 (many hosts can't route IPv6 to Gmail) and retries a
    couple of times, though on a cloud host that blocks SMTP this will still time out — which
    is exactly why Resend is the production path."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = recipient
    msg["Message-ID"] = message_id
    if REPLY_TO and REPLY_TO != FROM_EMAIL:
        msg["Reply-To"] = REPLY_TO
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(html, "html"))
    if attachment:
        filename, content = attachment
        part = MIMEApplication(content, Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    try:
        ipv4 = socket.getaddrinfo(SMTP_HOST, SMTP_PORT, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (socket.gaierror, IndexError):
        ipv4 = SMTP_HOST

    server = smtplib.SMTP(timeout=45)
    try:
        server.connect(ipv4, SMTP_PORT)
        server._host = SMTP_HOST
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [recipient], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send(
    subject: str, body_html: str, in_reply_to: str | None = None,
    attachment: tuple[str, bytes] | None = None, to: str | None = None,
) -> str | None:
    """Synchronous send — call via asyncio.to_thread from async code.
    attachment, if given, is (filename, content_bytes) — e.g. a generated brief PDF.
    Returns the Message-ID used for this send (None if the send was skipped/failed),
    so callers that need to correlate a later reply back to this specific email
    (e.g. duplicate-name flags) can record it."""
    # Reply to the owner who emailed; anything else (or a non-owner address) → primary owner.
    # This preserves the safety property that the agent only ever emails owners, never prospects.
    recipient = to if is_owner(to) else PRIMARY_OWNER
    if not recipient or not FROM_EMAIL:
        logger.warning("crm_mail: no recipient/from configured, skipping send")
        return None

    # Callers write plain "\n" for line breaks; convert so it actually renders in email clients.
    html = body_html.replace("\n", "<br>\n")
    message_id = make_msgid()

    if RESEND_API_KEY:
        transport, fn = "resend", _send_via_resend
    elif SMTP_HOST and SMTP_USER and SMTP_PASSWORD:
        transport, fn = "smtp", _send_via_smtp
    else:
        logger.warning("crm_mail: no transport configured (set RESEND_API_KEY), skipping send")
        return None

    import crm_store
    last_err: Exception | None = None
    # Retry a couple of times — a single transient network/API blip shouldn't lose a reply
    # the agent already produced.
    for attempt in range(1, 4):
        try:
            fn(subject, html, recipient, message_id, in_reply_to, attachment)
            crm_store.log_event("reply_sent", {"to": recipient, "subject": subject, "via": transport})
            return message_id
        except Exception as e:
            last_err = e
            logger.warning(f"crm_mail: {transport} send attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                import time
                time.sleep(2 * attempt)
    logger.error(f"crm_mail: send failed after retries ({transport}): {last_err}")
    crm_store.log_event("reply_failed", {"subject": subject, "error": str(last_err), "via": transport})
    return None


async def send_async(
    subject: str, body_html: str, in_reply_to: str | None = None,
    attachment: tuple[str, bytes] | None = None, to: str | None = None,
) -> str | None:
    import asyncio
    return await asyncio.to_thread(send, subject, body_html, in_reply_to, attachment, to)
