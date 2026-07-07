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

# Edit this to match how you actually write — short examples help Claude match your voice.
VOICE_PROFILE = """Direct, warm, no corporate filler. Short sentences. Doesn't over-explain or \
over-apologize. Gets to the point in the first line. Signs off simply."""

DRAFT_SYSTEM_PROMPT = f"""You draft a single email on behalf of Joe Azzaro, a fund manager at Cedar Ridge Capital raising capital.

Use the contact's full profile and interaction history — reference real prior conversation, don't write generically.

Critical constraints:
- NEVER pitch something they've already passed on
- NEVER ignore an objection — if they raised a fee concern, don't pretend it doesn't exist; address it or work around it
- If they've expressed what they care about, lead with that angle
- If there are active objections in the objection profile, the draft should either address them directly or be structured to move past them
- Match this voice: {VOICE_PROFILE}

Output only the email body (no subject line, no commentary, no "Here's a draft:" preamble).
"""


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
        system=DRAFT_SYSTEM_PROMPT + crm_store.directives_prompt_block(),
        messages=[{
            "role": "user",
            "content": f"Profile:\n{profile}\nHistory:\n{history_text}\n\nInstruction: {instruction}",
        }],
    )
    draft = resp.content[0].text.strip()
    return f"Draft for {person.get('name') or person['email']} (review before sending):\n\n{draft}"
