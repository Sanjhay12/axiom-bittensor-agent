"""
Phase 5 — Drafting assistant. Generates a draft email using relationship context,
sent to Telegram for the user's approval. There is no send capability here by
design — this module only ever produces text for the user to copy and send themselves.
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

# Last-resort fallback, only used when there's nothing of Joe's own writing to learn from
# yet. Normally the voice is learned automatically from his real sent emails (see
# resolve_voice), or from samples he's explicitly pasted (the 'voice_profile' config key).
DEFAULT_VOICE = """Direct, warm, no corporate filler. Short sentences. Doesn't over-explain or \
over-apologize. Gets to the point in the first line. Signs off simply."""

# Extracts ONLY Joe's own prose out of his mail, so forwarded investor<->Joe threads (which
# arrive From: him but contain both voices) become clean voice samples instead of a blend.
VOICE_EXTRACT_SYSTEM = """You are given several raw email bodies that Joe Azzaro — a fund manager \
raising capital — either sent or forwarded. Each may be a full back-and-forth thread: Joe's own \
messages mixed with quoted replies from investors, forwarded headers, signatures, and disclaimers.

Extract ONLY the text Joe himself wrote, VERBATIM, so his writing style can be studied. For each \
distinct message Joe authored:
- Keep his exact words, including his greeting and sign-off. Do NOT paraphrase, summarize, \
translate, correct, or clean up his wording — reproduce it as-is.
- Drop everything he did NOT write: quoted or prior messages from other people, "On <date> ... \
wrote:" blocks, lines starting with ">", forwarded headers (From:/Sent:/To:/Subject:/Date:), \
email signatures, legal/confidentiality disclaimers, and "Sent from my iPhone"-type footers.

Joe's own messages are the ones written from his point of view TO the investor (not the investor \
writing to him). If a body contains none of Joe's own writing, skip it entirely.

Output each of Joe's messages as its own block, with blocks separated by a line containing only:
~~~
Output nothing else — no numbering, no commentary, no preamble. If none of the bodies contain any \
writing by Joe, output nothing at all."""

_VOICE_BLOCK_SEP = re.compile(r"(?m)^\s*~~~\s*$")
_VOICE_CACHE_KEY = "voice_learned"      # cached distilled samples + the ts they were built from
_MAX_LEARN_MESSAGES = 8                 # how many of his recent messages to learn from

DRAFT_SYSTEM_PROMPT = """You draft a single email on behalf of Joe Azzaro, a fund manager at Cedar Ridge Capital raising capital.

Use the contact's full profile and interaction history — reference real prior conversation, don't write generically.

Critical constraints:
- NEVER pitch something they've already passed on
- NEVER ignore an objection — if they raised a fee concern, don't pretend it doesn't exist; address it or work around it
- If they've expressed what they care about, lead with that angle
- If there are active objections in the objection profile, the draft should either address them directly or be structured to move past them

VOICE — write EXACTLY as Joe writes. Below are samples of his own emails and/or notes on his style; study them and mirror his tone, greeting, sentence length, vocabulary, level of formality, and sign-off. Do not sound like a generic AI assistant:
---
{voice}
---

Output only the email body (no subject line, no commentary, no "Here's a draft:" preamble).
"""


async def _extract_owner_prose(bodies: list[str]) -> str | None:
    """Runs the extraction pass over Joe's raw message bodies and returns just his own writing,
    verbatim, blocks joined with a plain separator. None if nothing of his could be extracted."""
    numbered = "\n\n".join(f"=== EMAIL {i + 1} ===\n{b}" for i, b in enumerate(bodies))
    resp = await claude.messages.create(
        model=MODEL, max_tokens=1500, system=VOICE_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": numbered}],
    )
    text = resp.content[0].text.strip()
    blocks = [b.strip() for b in _VOICE_BLOCK_SEP.split(text) if b.strip()]
    return "\n\n---\n\n".join(blocks) if blocks else None


async def _learned_voice() -> tuple[str | None, int]:
    """(distilled samples of Joe's own writing, number of source messages). Cached in config and
    only rebuilt when newer mail of his has arrived, so per-draft cost is a cheap read, not an
    LLM call. Returns (None, n) when nothing of his writing could be learned yet."""
    rows = crm_store.recent_owner_messages(_MAX_LEARN_MESSAGES)
    if not rows:
        return None, 0
    latest_ts, n = rows[0]["ts"], len(rows)

    cached = crm_store.get_config(_VOICE_CACHE_KEY)
    if cached:
        try:
            data = json.loads(cached)
            if data.get("built_from_ts") == latest_ts and data.get("text"):
                return data["text"], data.get("count", n)
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        text = await _extract_owner_prose([r["raw_excerpt"] for r in rows])
    except Exception as e:
        logger.warning(f"crm_draft: voice extraction failed, falling back: {e}")
        return None, n
    if not text:
        return None, n
    crm_store.set_config(_VOICE_CACHE_KEY, json.dumps({"text": text, "built_from_ts": latest_ts, "count": n}))
    return text, n


async def resolve_voice() -> tuple[str, str]:
    """(source, samples) for the voice a draft will currently write in. Precedence:
      1. an explicit profile Joe pasted ("voice: ..."), if any — a manual override always wins;
      2. otherwise learned from his own recent mail, with only HIS prose extracted from each
         (so forwarded investor threads don't blend the two voices);
      3. otherwise the built-in default, until there's any of his writing to learn from.
    The 'source' string is human-readable for the "voice" command."""
    explicit = crm_store.get_config("voice_profile")
    if explicit:
        return "samples you pasted", explicit
    learned, n = await _learned_voice()
    if learned:
        return f"learned from {n} of your own emails", learned
    return "built-in default (no sent emails on file yet)", DEFAULT_VOICE


async def _voice() -> str:
    """The samples/style text injected into the draft prompt — see resolve_voice."""
    return (await resolve_voice())[1]


async def generate(query: str, instruction: str) -> str:
    person = crm_store.find_person(query)
    if not person:
        return f"No contact matching '{query}'."

    interactions = crm_store.list_interactions(person["id"])
    history_text = "\n".join(
        f"- [{i['direction']}] {i.get('subject') or ''}: {i.get('summary') or ''}"
        for i in interactions
    ) or "No recorded interactions yet."

    def _opt(label, val):
        return f"{label}: {val}\n" if val else ""

    raw_obj = person.get("objection_profile")
    try:
        obj_profile = json.loads(raw_obj) if isinstance(raw_obj, str) else (raw_obj or {})
    except (json.JSONDecodeError, TypeError):
        obj_profile = {}
    obj_lines = "\n".join(f"  {k}: {v}" for k, v in obj_profile.items() if v)

    profile = (
        f"Name: {person.get('name') or person['email']}\n"
        f"Firm: {person.get('firm_name') or 'unknown'}\n"
        + _opt("Role", person.get("role"))
        + _opt("Investor type", person.get("investor_type"))
        + f"Stage: {person.get('stage')}\n"
        + _opt("Warmth", person.get("warmth"))
        + _opt("Mandate / focus", person.get("mandate"))
        + _opt("What they care about", person.get("cares_about"))
        + _opt("Liked products", person.get("liked_products"))
        + _opt("Previously passed on", person.get("passed_on"))
        + _opt("Objections (summary)", person.get("objections"))
        + (f"Objection profile:\n{obj_lines}\n" if obj_lines else "")
        + _opt("Move-forward conditions", person.get("move_forward_conditions"))
        + _opt("Communication style", person.get("communication_style"))
        + _opt("Personal notes", person.get("personal_notes"))
        + _opt("Next step", person.get("next_step"))
    )

    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=500,
        system=DRAFT_SYSTEM_PROMPT.format(voice=await _voice()) + crm_store.directives_prompt_block(),
        messages=[{
            "role": "user",
            "content": f"Profile:\n{profile}\nHistory:\n{history_text}\n\nInstruction: {instruction}",
        }],
    )
    draft = resp.content[0].text.strip()
    return f"Draft for {person.get('name') or person['email']} (review before sending):\n\n{draft}"
