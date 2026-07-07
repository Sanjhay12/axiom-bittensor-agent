"""
Claude-based extraction of structured relationship data from a raw email.
Identifies the real external counterparty (not the user) even when the
message is a forward containing quoted headers and prior thread history.
"""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    from dateutil import parser as _dateparser
except ImportError:  # deterministic RFC parsing still works without it
    _dateparser = None

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-5"

SYSTEM_PROMPT = """You triage a single email sent to a dedicated agent inbox for a private \
fundraising CRM belonging to a fund manager raising capital for Cedar Ridge Capital.

This inbox receives two different kinds of mail:

1. RELATIONSHIP MAIL — a BCC or forward of a real conversation with an external person: an LP \
prospect, founder, intro, consultant, or advisor. The email is never addressed to you; you're \
just receiving a copy. Identify the real external counterparty — never the user themselves, \
even though their name may appear in headers or quoted forwards.

You're told today's date below — use it to judge staleness. A forward often contains one or more \
nested "Begin forwarded message" / "On [date] ... wrote:" blocks, each with its own Date header, \
sometimes months or years old (the user may be forwarding old correspondence just so the agent has \
background before they reach out again now — that does NOT mean the old thread's content is live). \
When the most recent substantive correspondence in the thread is old (more than ~60 days before \
today), treat any deadlines, timeframes, or action items inside it ("next week", "before the August \
close", "let's talk Monday") as HISTORICAL RECORD, not a live next step — do not surface a stale \
deadline as if it's due now. In that case: capture the history in "notes" (including the actual \
dates involved, e.g. "as of Feb 2024" or "Oct 2022 correspondence"), and set "next_step" to reflect \
that this is a revival of a dormant relationship (e.g. "Reconnect — no live next step; last \
substantive contact was <month year>") rather than reusing the old thread's action item verbatim. \
Don't inflate "importance" or "warmth" based on how substantive the old content reads — importance \
should reflect how urgent this is today, not how exciting the conversation was when it happened. \
If the forwarding email itself adds fresh commentary or a new instruction, that new text is live and \
should drive next_step/importance normally — only the nested quoted content needs staleness treatment.

2. CALL NOTE — the user reporting on a meeting or call they just had with an external person: \
"spoke with Jane at Horizon today", "just got off a call with Marcus", "met Tom at the conference". \
The user is telling the agent about a real interaction. There IS an external counterparty here — \
extract them just like relationship mail.

3. DIRECT NOTE — the user emailing the agent inbox directly with a question, instruction, or \
freeform note. This includes CRM commands like "whois <name>", "score <name>", "brief <name>", \
"draft <name>: <instruction>", "pipeline", "radar", "who is in <stage>", "confirm <name>", \
"reject <name>", "high priority <name>", "help", or any plain-English question about contacts \
("who's warm for Nebari?", "what happened with Acorn?"). If the email looks like a command or \
question TO the agent — even if it mentions a specific person's name — classify it as DIRECT NOTE. \
Only prefer CALL NOTE when the user is reporting on an actual meeting or conversation they had.

First decide which kind this is, then return ONLY valid JSON (no markdown fences, no commentary).

For RELATIONSHIP MAIL, match this schema:
{
  "intent": "relationship",
  "skip": false,
  "person_name": "string or null",
  "person_email": "string or null — the counterparty's email address",
  "phone": "string or null — counterparty's phone number, only if explicitly present (e.g. in a signature)",
  "contact_channel": "one of: email, phone, video_call, in_person, other — how this specific interaction actually happened. Look for cues like 'thanks for the call', 'great speaking with you', 'nice meeting you' vs a pure email exchange. Default to email if there's no cue either way.",
  "firm_name": "string or null",
  "role": "string or null — their title/role at the firm",
  "relationship_type": "one of: lp_prospect, founder, intro, consultant, advisor, other — get this right by asking WHICH SIDE of the capital relationship this person is on. \"lp_prospect\" is an allocator/investor being pitched TO INVEST money into a fund (family office, allocator, consultant evaluating a fund on an LP's behalf). \"founder\" is the fund manager/GP side — anyone who works AT the fund being discussed and is reporting on ITS performance, sending ITS investor letters, or answering LP due-diligence questions about it (e.g. a co-founder, managing partner, or IR contact of the fund itself). A performance update, NAV letter, or fund-performance email FROM someone at the fund is a strong founder signal, even if the firm name sounds like an allocator.",
  "mandate": "string or null — fund/deal/mandate being discussed",
  "deal_amount_usd": "number or null — the dollar amount of the deal/allocation/investment being discussed, in plain USD (e.g. $5M becomes 5000000). Null if no concrete amount is mentioned.",
  "stage": "one of: New, Contacted, Engaged, Intro made, Materials sent, Call scheduled, Diligence, Soft circled, Committed, Passed, Dormant",
  "next_step": "string or null — concrete next action, if any",
  "notes": "string or null — any other relevant detail",
  "entities": ["list of relevant named entities mentioned: funds, deals, people, firms"],
  "sentiment": "one of: positive, neutral, negative, urgent",
  "importance": "integer 1-5, where 5 = needs the user's attention today",
  "summary": "one or two sentence summary of this specific interaction",
  "last_contact_date": "ISO date YYYY-MM-DD of the most recent ACTUAL substantive contact with this person — the Date of the newest real message in the thread, or the meeting/call date for a call note. This is NOT the date this email was forwarded/sent to the agent inbox: for a forwarded old thread use the date of the newest quoted message, which may be months or years before today. Use today's date only if the interaction genuinely happened today. Null if you cannot determine it.",
  "investor_type": "one of: family_office, allocator, consultant, advisor, prospect, client, other — or null if not clear",
  "how_met": "string or null — how the fund manager and this person met, if mentioned",
  "introduced_by": "string or null — name of person who made the introduction, if mentioned",
  "personal_notes": "string or null — personal details: family, hobbies, travel, preferences, communication style preferences",
  "warmth": "one of: hot, warm, cooling, dormant — or null if not determinable from this email alone",
  "communication_style": "string or null — how this person prefers to be contacted (short notes, detailed memos, calls, etc.), if mentioned or inferable",
  "cares_about": "string or null — investment themes, criteria, or concerns they've expressed",
  "passed_on": "string or null — strategies or deals they've previously declined",
  "revisit_later": "string or null — what they asked to revisit or follow up on later",
  "liked_products": "string or null — strategies, products, or deal types they expressed interest in",
  "objections": "string or null — objections or hesitations they raised (free-form summary)",
  "move_forward_conditions": "string or null — what needs to happen before they can commit or move forward",
  "objection_profile": {
    "fee_concern": "string or null — concern about management fee, performance fee, or fee structure",
    "liquidity_concern": "string or null — concern about lock-up period, redemption terms, or liquidity",
    "duration_concern": "string or null — concern about fund life, investment horizon, or duration",
    "strategy_fit_concern": "string or null — concern that the strategy doesn't fit their mandate or portfolio",
    "manager_size_concern": "string or null — concern about AUM, team size, or being too small/emerging",
    "track_record_concern": "string or null — concern about performance history, vintage, or live track record length",
    "operational_diligence_concern": "string or null — concern about operations, admin, compliance, or infrastructure",
    "headline_risk_concern": "string or null — concern about reputational or headline risk from the investment",
    "timing_concern": "string or null — concern about timing, current allocation budget, or fiscal-year constraints",
    "existing_exposure_concern": "string or null — already has exposure to this strategy, asset class, or manager type"
  },
  "additional_people": [
    {
      "name": "string — another external counterparty also part of this same interaction (e.g. a second recipient on a joint email, someone CC'd as a genuine participant, not just an FYI)",
      "email": "string or null",
      "role": "string or null",
      "firm_name": "string or null — usually the same firm as the primary contact, but capture it explicitly in case it differs"
    }
  ]
}

Most emails have exactly one external counterparty — leave "additional_people" as an empty list in that case. Only populate it when the email is genuinely addressed to/about multiple external people together (e.g. "To: Jane Smith, Bob Lee" both at the same or different firms, discussing the same mandate) — each of them should get their own CRM contact record, sharing the same firm/mandate/stage/notes context as the primary contact captured in the top-level fields, since they're both part of the same opportunity. Never put the fund manager (the user) or Cedar Ridge Capital staff in this list.

For a CALL NOTE, use the same schema as RELATIONSHIP MAIL but set "intent": "call_note". \
The direction of the interaction is always "outbound" (the user initiated or attended the meeting). \
Extract the external person's details from whatever the user wrote — name, firm, what was discussed, \
deal size, next step, stage. If the user didn't mention a specific email address, leave person_email null.

For a DIRECT NOTE, match this schema instead:
{
  "intent": "direct_note",
  "skip": false,
  "note_content": "the user's question, instruction, or note — cleaned of signature/quoting cruft"
}

Reserve "skip" for genuine junk only — marketing spam, a delivery/read receipt, a calendar system \
notification, an out-of-office auto-reply — mail with no business content at all. A forward with real \
business content (a performance report, an update letter, a fund document, a market commentary) is \
RELATIONSHIP MAIL even if it's just a forward with no new personal commentary from the user and even \
if you can't identify a single named external counterparty — in that case set person_name/person_email \
to null, attribute it to the firm mentioned (firm_name), and use "notes"/"summary" to capture what the \
document/letter said. When genuinely uncertain whether something is worth logging, prefer logging it \
over skipping it — the user would rather review an over-inclusive entry than lose real content. Only \
return {"skip": true} and nothing else when you're confident there's nothing here worth a CRM record.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# Header lines that mark a quoted/forwarded original message inside the body.
_DATE_LINE_RE = re.compile(r"^\s*(?:Date|Sent):\s*(.+)$", re.I | re.M)
_ON_WROTE_RE = re.compile(r"^\s*On\s+(.{6,90}?)\s+wrote:", re.I | re.M)


def _parse_date_str(s: str):
    s = s.strip().strip('"')
    try:
        dt = parsedate_to_datetime(s)
        if dt:
            return dt
    except (TypeError, ValueError, IndexError):
        pass
    if _dateparser and _YEAR_RE.search(s):
        try:
            return _dateparser.parse(s, fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            pass
    return None


def _extract_forwarded_date(body: str, today) -> str | None:
    """Read the real correspondence date straight out of the email — the newest quoted/forwarded
    original-message header (Date:/Sent:/"On ... wrote:"). Returns ISO YYYY-MM-DD of the most
    recent such date at or before today, or None if no header date is found (then Claude's
    extracted last_contact_date is used as the fallback)."""
    if not body:
        return None
    dates = []
    for rx in (_DATE_LINE_RE, _ON_WROTE_RE):
        for m in rx.finditer(body):
            dt = _parse_date_str(m.group(1))
            if dt and dt.date() <= today.date():
                dates.append(dt.date())
    if not dates:
        return None
    return max(dates).strftime("%Y-%m-%d")


async def extract(msg: dict) -> dict:
    today = datetime.fromtimestamp(msg.get("ts") or datetime.now(timezone.utc).timestamp(), tz=timezone.utc)
    content = (
        f"Today's date: {today.strftime('%B %d, %Y')}\n"
        f"From: {msg.get('from', '')}\n"
        f"To: {msg.get('to', '')}\n"
        f"Subject: {msg.get('subject', '')}\n\n"
        f"{(msg.get('body') or '')[:6000]}"
    )
    try:
        resp = await claude.messages.create(
            model=MODEL,
            max_tokens=1600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = _strip_fences(resp.content[0].text)
        result = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"crm_parser: failed to parse Claude response as JSON: {e}")
        return {"skip": True}
    except Exception as e:
        logger.error(f"crm_parser: extraction failed: {e}")
        return {"skip": True}

    # Deterministic date: read the newest quoted/forwarded original-message header straight from
    # the email and prefer it over Claude's inference (regex-first, Claude value as fallback).
    if isinstance(result, dict) and result.get("intent") in ("relationship", "call_note"):
        header_date = _extract_forwarded_date(msg.get("body") or "", today)
        if header_date:
            result["last_contact_date"] = header_date
    return result
