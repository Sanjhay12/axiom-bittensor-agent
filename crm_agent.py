"""
Cedar Ridge Inbox Agent — Phase 1: dedicated inbox ingestion + relationship memory.
Polls agent@cedarridge.capital, extracts structured relationship data with Claude,
and writes person/firm/interaction records. Everything the agent needs to tell the
user — alerts, digests, confirmations, replies — goes out as email (crm_mail.py),
not Telegram. The same inbox doubles as the command line: emailing the agent
"brief jane@x.com" or "confirm jane@x.com" works the same as the Telegram commands.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re

import crm_ask
import crm_brief
import crm_draft
import crm_enrich
import crm_import
import crm_mail
import crm_mailbox
import crm_parser
import crm_radar
import crm_score
import crm_store

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("CRM_POLL_INTERVAL_SECONDS", 15 * 60))
HIGH_IMPORTANCE_THRESHOLD = 4

_PIPELINE_RE = re.compile(r"^pipeline\s*$", re.I)
_RADAR_RE = re.compile(r"^radar\s*$", re.I)
_CONFIRM_RE = re.compile(r"^confirm\s+(.+)$", re.I)
_REJECT_RE = re.compile(r"^reject\s+(.+)$", re.I)
_WHOIS_RE = re.compile(r"^whois\s+(.+)$", re.I)
_SCORE_RE = re.compile(r"^score\s+(.+)$", re.I)
_BRIEF_RE = re.compile(r"^brief\s+(.+)$", re.I)
_DRAFT_RE = re.compile(r"^draft\s+([^:]+):?\s*(.*)$", re.I)
_WHO_IN_RE = re.compile(r"^who(?:'s|s|\s+is|\s+are)\s+in\s+(.+)$", re.I)
_HIGH_PRIORITY_RE = re.compile(r"^(?:mark\s+|set\s+)?high.?priority\s+(.+)$", re.I)
_UNHIGH_PRIORITY_RE = re.compile(r"^(?:remove|unset|clear)\s+high.?priority\s+(.+)$", re.I)
_HELP_RE = re.compile(r"^(?:help|commands|\?)\s*$", re.I)

HELP_TEXT = """<b>Cedar Ridge Inbox Agent — commands</b>

You can email any of these, or just forward/BCC a relationship email and it gets parsed automatically.

<b>pipeline</b> — every contact grouped by stage, with deal size and next step
<b>radar</b> — run the follow-up digest on demand
<b>whois &lt;name or email&gt;</b> — full profile: phone, email, firm, stage, channel, notes
<b>score &lt;name or email&gt;</b> — LP score (0-100) with the full signal breakdown
<b>brief &lt;name or email&gt;</b> — one-page pre-call brief
<b>draft &lt;name or email&gt;: &lt;instruction&gt;</b> — draft a reply for your review (never auto-sent)
<b>who is in &lt;stage&gt;</b> — e.g. "who is in diligence", "who's in engaged"
<b>high priority &lt;name or email&gt;</b> — always flag this contact in the daily digest
<b>remove high priority &lt;name or email&gt;</b> — undo that
<b>confirm &lt;name or email&gt;</b> / <b>reject &lt;name or email&gt;</b> — accept or dismiss a pending pipeline stage change
<b>help</b> — this list

You can also just ask in plain English, e.g. "who's warm for Nebari?" or "what happened with Acorn?"

To bulk-add contacts, email this inbox an Excel (.xlsx) or CSV attachment with columns like Name, Email, Phone, Firm, Stage, Deal Amount.
"""


def _format_enrichment(person: dict) -> str:
    raw = person.get("enrichment")
    if not raw:
        return "Enrichment: none yet"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return "Enrichment: none yet"

    parts = []
    linkedin = data.get("linkedin") or {}
    if linkedin:
        url = linkedin.get("url") or linkedin.get("linkedin_profile_url")
        parts.append(f"LinkedIn: {url}" if url else "LinkedIn: profile data found")
    news = (data.get("news") or {}).get("summary")
    if news:
        parts.append(f"Recent news: {news}")
    funding = (data.get("funding") or {}).get("summary")
    if funding:
        parts.append(f"Funding history: {funding}")
    return "\n".join(parts) if parts else "Enrichment: none yet"


async def _handle_command(note: str) -> str | None:
    """Recognizes the same command verbs as the Telegram bot. Returns None to fall through to free-form Q&A."""
    note = note.strip()

    if _HELP_RE.match(note):
        return HELP_TEXT
    if _PIPELINE_RE.match(note):
        return crm_ask.pipeline_summary()
    if _RADAR_RE.match(note):
        return crm_radar.build_digest() or "Nothing to flag right now."

    m = _CONFIRM_RE.match(note)
    if m:
        return crm_ask.confirm_stage(m.group(1))
    m = _REJECT_RE.match(note)
    if m:
        return crm_ask.reject_stage(m.group(1))

    m = _WHOIS_RE.match(note)
    if m:
        person = crm_store.find_person(m.group(1))
        if not person:
            return f"No contact matching '{m.group(1)}'."
        return (
            f"{person.get('name') or person['email']}\n"
            f"Email: {person['email']}\n"
            f"Phone: {person.get('phone') or 'unknown'}\n"
            f"Firm: {person.get('firm_name') or 'unknown'}\n"
            f"Stage: {person.get('stage')}\n"
            f"Relationship: {person.get('relationship_type') or 'unknown'}\n"
            f"Mandate: {person.get('mandate') or 'none noted'}\n"
            f"Connected via: {person.get('contact_channel') or 'email'}\n"
            f"High priority: {'yes' if person.get('manual_priority') else 'no'}\n"
            f"Next step: {person.get('next_step') or 'none noted'}\n"
            f"Notes: {person.get('notes') or 'none'}\n"
            f"{_format_enrichment(person)}"
        )

    m = _WHO_IN_RE.match(note)
    if m:
        return crm_ask.stage_filter(m.group(1))

    m = _UNHIGH_PRIORITY_RE.match(note)
    if m:
        return crm_ask.set_priority(m.group(1), False)
    m = _HIGH_PRIORITY_RE.match(note)
    if m:
        return crm_ask.set_priority(m.group(1), True)

    m = _SCORE_RE.match(note)
    if m:
        result = crm_score.score_by_query(m.group(1))
        if not result:
            return f"No contact matching '{m.group(1)}'."
        breakdown = "\n".join(
            f"  {k}: {v if v is not None else 'no data'}" for k, v in result["breakdown"].items()
        )
        return f"{result['name']} ({result.get('firm_name') or 'unknown firm'}): {result['composite_score']}/100\n{breakdown}"

    m = _BRIEF_RE.match(note)
    if m:
        return await crm_brief.generate(m.group(1))

    m = _DRAFT_RE.match(note)
    if m:
        query, instruction = m.group(1).strip(), m.group(2).strip()
        return await crm_draft.generate(query, instruction or "Write a friendly check-in follow-up.")

    return None


async def _reply_to_note(msg: dict, note: str):
    try:
        reply = await _handle_command(note)
        if reply is None:
            reply = await crm_ask.answer(note)
    except Exception as e:
        logger.error(f"crm_agent: failed to answer direct note: {e}")
        reply = "Got your note but hit an error processing it. Logged for now."

    subject = f"Re: {msg.get('subject') or 'your note'}"
    body = f"<b>You wrote:</b> {note}\n\n{reply}"
    await crm_mail.send_async(subject, body, in_reply_to=msg.get("message_id"))


async def _handle_attachments(msg: dict) -> bool:
    """If this email has a recognized contact-list attachment, import it and reply
    with a summary instead of running it through relationship/note extraction.
    Returns True if an attachment was handled (caller should skip the rest)."""
    attachments = msg.get("attachments") or []
    if not attachments:
        return False

    def _schedule_enrichment(person_id, extracted):
        asyncio.create_task(crm_enrich.enrich_person(person_id, extracted))

    summaries = []
    for att in attachments:
        try:
            result = crm_import.import_contacts(att["content"], att["filename"], on_new_person=_schedule_enrichment)
            summaries.append(
                f"{att['filename']}: {result['added']} added, {result['updated']} updated, "
                f"{result['skipped']} skipped (of {result['total']} rows)"
            )
        except Exception as e:
            logger.error(f"crm_agent: import failed for {att['filename']}: {e}")
            summaries.append(f"{att['filename']}: import failed — {e}")

    body = "<b>Contact import results:</b>\n" + "\n".join(summaries)
    await crm_mail.send_async(f"Re: {msg.get('subject') or 'contact import'}", body, in_reply_to=msg.get("message_id"))
    return True


async def process_once():
    messages = crm_mailbox.fetch_new_messages()
    for msg in messages:
        if await _handle_attachments(msg):
            continue

        extracted = await crm_parser.extract(msg)
        if extracted.get("skip"):
            continue

        if extracted.get("intent") == "direct_note":
            note = extracted.get("note_content") or msg.get("body", "")
            logger.info(f"crm_agent: direct note received: {note[:200]}")
            await _reply_to_note(msg, note)
            continue

        firm_id = crm_store.get_or_create_firm(extracted.get("firm_name"))
        person_id, is_new_person = crm_store.upsert_person(extracted, firm_id, msg["ts"])
        is_new_interaction = crm_store.insert_interaction(
            person_id, msg["message_id"], msg["subject"], msg["direction"],
            msg["ts"], extracted, msg.get("body", ""),
        )

        if not is_new_interaction:
            continue

        logger.info(
            f"crm_agent: recorded interaction with {extracted.get('person_name') or extracted.get('person_email')} "
            f"(importance={extracted.get('importance')})"
        )

        if (extracted.get("importance") or 0) >= HIGH_IMPORTANCE_THRESHOLD:
            name = extracted.get("person_name") or extracted.get("person_email") or "Unknown"
            firm = extracted.get("firm_name") or ""
            label = f"{name} ({firm})" if firm else name
            body = (
                f"<b>High-priority contact: {label}</b>\n"
                f"{extracted.get('summary', '')}\n"
                f"Next step: {extracted.get('next_step') or 'none noted'}"
            )
            await crm_mail.send_async(f"High-priority contact: {label}", body)

        person = crm_store.find_person(extracted.get("person_email") or "")
        if person and person.get("pending_stage"):
            await crm_mail.send_async(
                f"Stage change pending: {person.get('name') or person['email']}",
                f"<b>{person.get('name') or person['email']}</b>: {person['stage']} → {person['pending_stage']}\n"
                f"Reason: {person.get('pending_stage_reason') or 'n/a'}\n\n"
                f"Reply to this email with \"confirm {person['email']}\" or \"reject {person['email']}\".",
            )

        if is_new_person:
            asyncio.create_task(crm_enrich.enrich_person(person_id, extracted))


async def run_loop():
    crm_store.init_crm_db()
    logger.info(f"crm_agent: starting poll loop every {POLL_INTERVAL}s")
    while True:
        try:
            await process_once()
        except Exception as e:
            logger.error(f"crm_agent: loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL)
