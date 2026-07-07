"""
IMAP polling for the dedicated agent inbox (agent@cedarridge.capital).
The user BCCs or forwards relationship emails here — this module fetches
unseen messages and normalizes them for crm_parser.
"""
from __future__ import annotations
import email
import email.utils
import imaplib
import logging
import os
import time
from datetime import datetime, timedelta
from email.header import decode_header

logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("CRM_IMAP_HOST")
IMAP_USER = os.getenv("CRM_IMAP_USER")
IMAP_PASSWORD = os.getenv("CRM_IMAP_PASSWORD")
OWNER_EMAILS = [e.strip().lower() for e in os.getenv("OWNER_EMAIL", "").split(",") if e.strip()]


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    return "".join(
        part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part
        for part, enc in parts
    )


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disp:
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception:
        return ""


import re as _re

_QUOTE_PATTERNS = _re.compile(
    r"(^On .+wrote:\s*$|^>.*$|^-{3,}.*Original Message.*-{3,}$|^_{5,}$|^From:\s+)",
    _re.MULTILINE | _re.IGNORECASE,
)


def _strip_quoted_reply(body: str) -> str:
    """For reply emails, return only the content above the first quoted block."""
    lines = body.splitlines()
    cutoff = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped.startswith(">")
            or _re.match(r"^On .+ wrote:$", stripped, _re.IGNORECASE)
            or _re.match(r"^-{3,}", stripped)
            or _re.match(r"^_{5,}", stripped)
            or _re.match(r"^From:\s+", stripped, _re.IGNORECASE)
        ):
            cutoff = i
            break
    return "\n".join(lines[:cutoff]).strip()


SPREADSHEET_EXTENSIONS = (".xlsx", ".csv", ".numbers")
DOCUMENT_EXTENSIONS = (".pdf",)
AUDIO_EXTENSIONS = (".m4a", ".mp3", ".wav", ".ogg", ".oga", ".mp4", ".webm", ".aac", ".amr")
ATTACHMENT_EXTENSIONS = SPREADSHEET_EXTENSIONS + DOCUMENT_EXTENSIONS + AUDIO_EXTENSIONS


def _extract_attachments(msg: email.message.Message) -> list[dict]:
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        disp = str(part.get("Content-Disposition") or "")
        filename = part.get_filename()
        if not filename or "attachment" not in disp.lower():
            continue
        filename = _decode(filename)
        if not filename.lower().endswith(ATTACHMENT_EXTENSIONS):
            continue
        try:
            content = part.get_payload(decode=True)
            if content:
                attachments.append({"filename": filename, "content": content})
        except Exception:
            continue
    return attachments


def fetch_new_messages() -> list[dict]:
    """Fetches recent messages in the agent inbox — a rolling window, NOT just unseen.
    Something upstream (a Gmail 'mark as read' filter, or a second client on the mailbox)
    can mark mail read before we poll, which would hide it from an UNSEEN search and
    silently drop it. crm_store.claim_message() (called before processing) is the real
    dedup guard, so re-seeing already-processed mail is harmless — correctness does not
    depend on the \\Seen flag."""
    if not (IMAP_HOST and IMAP_USER and IMAP_PASSWORD):
        logger.warning("crm_mailbox: CRM_IMAP_HOST/USER/PASSWORD not configured, skipping poll")
        return []

    messages = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select("INBOX")
        # Rolling recent window instead of UNSEEN — read-state can't hide mail from us.
        since = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
        status, data = conn.search(None, "SINCE", since)
        if status != "OK" or not data or not data[0]:
            return []

        for num in data[0].split():
            try:
                status, msg_data = conn.fetch(num, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                message_id = msg.get("Message-ID") or f"no-id-{num.decode()}-{int(time.time())}"
                subject = _decode(msg.get("Subject"))
                from_header = _decode(msg.get("From"))
                to_header = _decode(msg.get("To"))
                cc_header = _decode(msg.get("Cc"))
                # Prefer In-Reply-To; References' last entry is the immediate parent
                # when In-Reply-To is missing (some clients only set one or the other).
                in_reply_to = msg.get("In-Reply-To")
                if not in_reply_to and msg.get("References"):
                    in_reply_to = msg.get("References").split()[-1]

                try:
                    ts = int(email.utils.parsedate_to_datetime(msg.get("Date")).timestamp())
                except Exception:
                    ts = int(time.time())

                recipients = (to_header + cc_header).lower()
                direction = "bcc" if any(o in recipients for o in OWNER_EMAILS) else "forwarded"

                body = _extract_body(msg)
                if subject.lower().startswith("re:"):
                    body = _strip_quoted_reply(body)

                messages.append({
                    "message_id": message_id,
                    "subject": subject,
                    "from": from_header,
                    "to": to_header,
                    "cc": cc_header,
                    "ts": ts,
                    "body": body,
                    "direction": direction,
                    "in_reply_to": in_reply_to,
                    "attachments": _extract_attachments(msg),
                })
            except Exception as e:
                logger.error(f"crm_mailbox: failed to parse message {num}: {e}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    if messages:
        logger.info(f"crm_mailbox: fetched {len(messages)} new message(s)")
    return messages
