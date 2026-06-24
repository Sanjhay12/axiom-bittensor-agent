"""
Claude-based extraction of structured relationship data from a raw email.
Identifies the real external counterparty (not the user) even when the
message is a forward containing quoted headers and prior thread history.
"""
from __future__ import annotations
import json
import logging
import os

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

2. DIRECT NOTE — the user emailing the agent inbox directly, with no forwarded thread attached: \
a question ("who's warm for Nebari?"), an instruction, or a freeform note to remember. There is \
no external counterparty here — that's exactly how you tell it apart from relationship mail.

First decide which kind this is, then return ONLY valid JSON (no markdown fences, no commentary).

For RELATIONSHIP MAIL, match this schema:
{
  "intent": "relationship",
  "skip": false,
  "person_name": "string or null",
  "person_email": "string or null — the counterparty's email address",
  "firm_name": "string or null",
  "role": "string or null — their title/role at the firm",
  "relationship_type": "one of: lp_prospect, founder, intro, consultant, advisor, other",
  "mandate": "string or null — fund/deal/mandate being discussed",
  "stage": "one of: New, Contacted, Engaged, Intro made, Materials sent, Call scheduled, Diligence, Soft circled, Committed, Passed, Dormant",
  "next_step": "string or null — concrete next action, if any",
  "notes": "string or null — any other relevant detail",
  "entities": ["list of relevant named entities mentioned: funds, deals, people, firms"],
  "sentiment": "one of: positive, neutral, negative, urgent",
  "importance": "integer 1-5, where 5 = needs the user's attention today",
  "summary": "one or two sentence summary of this specific interaction"
}

For a DIRECT NOTE, match this schema instead:
{
  "intent": "direct_note",
  "skip": false,
  "note_content": "the user's question, instruction, or note — cleaned of signature/quoting cruft"
}

If the email is spam or an automated notification with no relationship content and no genuine \
note from the user, return {"skip": true} and nothing else.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


async def extract(msg: dict) -> dict:
    content = (
        f"From: {msg.get('from', '')}\n"
        f"To: {msg.get('to', '')}\n"
        f"Subject: {msg.get('subject', '')}\n\n"
        f"{(msg.get('body') or '')[:6000]}"
    )
    try:
        resp = await claude.messages.create(
            model=MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = _strip_fences(resp.content[0].text)
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"crm_parser: failed to parse Claude response as JSON: {e}")
        return {"skip": True}
    except Exception as e:
        logger.error(f"crm_parser: extraction failed: {e}")
        return {"skip": True}
