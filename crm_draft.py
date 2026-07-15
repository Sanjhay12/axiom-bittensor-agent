"""
Phase 5 — Drafting assistant. Generates a draft email using relationship context,
sent to Telegram for the user's approval. There is no send capability here by
design — this module only ever produces text for the user to copy and send themselves.
"""
from __future__ import annotations
import json
import os

from anthropic import AsyncAnthropic

import crm_store

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

# Last-resort fallback, only used when there's nothing of Joe's own writing to learn from
# yet. Normally the voice is learned automatically from his real sent emails (see
# voice_status), or from samples he's explicitly pasted (the 'voice_profile' config key).
DEFAULT_VOICE = """Direct, warm, no corporate filler. Short sentences. Doesn't over-explain or \
over-apologize. Gets to the point in the first line. Signs off simply."""

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


def voice_status() -> tuple[str, str]:
    """(source, samples) for the voice a draft will currently write in. Precedence:
      1. an explicit profile Joe pasted ("voice: ..."), if any — a manual override always wins;
      2. otherwise learned automatically from his own recent sent emails already on file;
      3. otherwise the built-in default, until there's any of his writing to learn from.
    The 'source' string is human-readable for the "voice" command."""
    explicit = crm_store.get_config("voice_profile")
    if explicit:
        return "samples you pasted", explicit
    samples = crm_store.recent_outbound_excerpts()
    if samples:
        joined = "\n\n---\n\n".join(samples)
        return f"learned from {len(samples)} of your own sent emails", joined
    return "built-in default (no sent emails on file yet)", DEFAULT_VOICE


def _voice() -> str:
    """The samples/style text injected into the draft prompt — see voice_status."""
    return voice_status()[1]


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
        system=DRAFT_SYSTEM_PROMPT.format(voice=_voice()) + crm_store.directives_prompt_block(),
        messages=[{
            "role": "user",
            "content": f"Profile:\n{profile}\nHistory:\n{history_text}\n\nInstruction: {instruction}",
        }],
    )
    draft = resp.content[0].text.strip()
    return f"Draft for {person.get('name') or person['email']} (review before sending):\n\n{draft}"
