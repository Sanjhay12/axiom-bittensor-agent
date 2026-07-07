"""
Level 1 "directives" — the owner steers the agent's behaviour by email.

A standing instruction ("always show deals over $1M first", "be more concise",
"stop nagging me about dormant contacts") is detected, stored, and injected into
the agent's prompts (see crm_store.directives_prompt_block, used by crm_todo /
crm_ask). Management verbs: email "directives" to list, "forget directive N" to remove.

Only owners can create directives (they change the agent's behaviour globally);
anyone may be answered, but only an owner's note becomes a standing instruction.
"""
from __future__ import annotations
import json
import logging
import os
import re

from anthropic import AsyncAnthropic

import crm_store

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

_LIST_RE = re.compile(r"^\s*(?:list\s+|show\s+)?directives\s*\??\s*$", re.I)
_FORGET_RE = re.compile(r"^\s*(?:forget|remove|delete|drop|clear)\s+directive\s+#?(\d+)\s*$", re.I)

CLASSIFY_PROMPT = """You decide whether a message from a fund manager to his CRM assistant is a STANDING INSTRUCTION about how the assistant should behave from now on.

Examples of standing instructions (is_directive = true):
- "always show deals over $1M first"
- "stop flagging dormant contacts in my dashboard"
- "group my dashboard by product, not by investor"
- "be more concise in your summaries"
- "always cc my assistant on drafts"

NOT standing instructions (is_directive = false) — return false for these:
- one-off questions: "who's warm for Nebari?", "what happened with Acorn?"
- data updates about a specific contact: "mark Jane high priority", "Acorn is now in diligence"
- requests to draft / brief / score / research a contact
- greetings, thanks, or chit-chat

Be conservative: only true when it's clearly a general, ongoing rule about the assistant's behaviour, not about one contact or one answer.

Return ONLY JSON, no prose: {"is_directive": true|false, "directive": "<clean imperative one-line rule, or null>"}"""


async def try_directive_command(note: str, from_owner: bool) -> str | None:
    """Returns a reply string if this note is a directive management/creation command,
    else None to fall through to normal Q&A."""
    note = note.strip()

    if _LIST_RE.match(note):
        ds = crm_store.get_active_directives()
        if not ds:
            return "No standing instructions set. Email me a rule like \"always show deals over $1M first\" and I'll remember it."
        body = "\n".join(f"  {d['id']}. {d['directive']}" for d in ds)
        return f"<b>Standing instructions</b>\n{body}\n\n(\"forget directive N\" to remove one.)"

    m = _FORGET_RE.match(note)
    if m:
        did = int(m.group(1))
        ok = crm_store.deactivate_directive(did)
        return f"Removed directive {did}." if ok else f"No active directive {did}."

    # Only owners can create standing instructions.
    if not from_owner:
        return None

    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=200, system=CLASSIFY_PROMPT,
            messages=[{"role": "user", "content": note}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        data = json.loads(text)
    except Exception as e:
        logger.error(f"crm_directives: classify failed: {e}")
        return None

    if not data.get("is_directive") or not data.get("directive"):
        return None

    directive = str(data["directive"]).strip()
    did = crm_store.add_directive(directive, None)
    return (
        f"Got it — I'll apply this from now on:\n  \"{directive}\"\n\n"
        f"(directive {did}; email \"directives\" to list them, \"forget directive {did}\" to undo.)"
    )
