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
from email.header import decode_header

logger = logging.getLogger(__name__)

IMAP_HOST = os.getenv("CRM_IMAP_HOST")
IMAP_USER = os.getenv("CRM_IMAP_USER")
IMAP_PASSWORD = os.getenv("CRM_IMAP_PASSWORD")
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "").lower()


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


SPREADSHEET_EXTENSIONS = (".xlsx", ".csv")


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
        if not filename.lower().endswith(SPREADSHEET_EXTENSIONS):
            continue
        try:
            content = part.get_payload(decode=True)
            if content:
                attachments.append({"filename": filename, "content": content})
        except Exception:
            continue
    return attachments


def fetch_new_messages() -> list[dict]:
    """Fetches and marks-as-read all unseen messages in the agent inbox."""
    if not (IMAP_HOST and IMAP_USER and IMAP_PASSWORD):
        logger.warning("crm_mailbox: CRM_IMAP_HOST/USER/PASSWORD not configured, skipping poll")
        return []

    messages = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(IMAP_USER, IMAP_PASSWORD)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
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

                try:
                    ts = int(email.utils.parsedate_to_datetime(msg.get("Date")).timestamp())
                except Exception:
                    ts = int(time.time())

                recipients = (to_header + cc_header).lower()
                direction = "bcc" if OWNER_EMAIL and OWNER_EMAIL in recipients else "forwarded"

                messages.append({
                    "message_id": message_id,
                    "subject": subject,
                    "from": from_header,
                    "to": to_header,
                    "cc": cc_header,
                    "ts": ts,
                    "body": _extract_body(msg),
                    "direction": direction,
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
