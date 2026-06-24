"""
Phase 5 — Drafting assistant. Generates a draft email using relationship context,
sent to Telegram for the user's approval. There is no send capability here by
design — this module only ever produces text for the user to copy and send themselves.
"""
from __future__ import annotations
import os

from anthropic import AsyncAnthropic

import crm_store

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

# Edit this to match how you actually write — short examples help Claude match your voice.
VOICE_PROFILE = """Direct, warm, no corporate filler. Short sentences. Doesn't over-explain or \
over-apologize. Gets to the point in the first line. Signs off simply."""

DRAFT_SYSTEM_PROMPT = f"""You draft a single email on behalf of a fund manager raising capital, \
to send to a specific contact in their pipeline. Use the contact's profile and interaction \
history for context — reference real prior conversation, don't write generically. Match this \
voice: {VOICE_PROFILE}

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

    profile = (
        f"Name: {person.get('name') or person['email']}\n"
        f"Firm: {person.get('firm_name') or 'unknown'}\n"
        f"Stage: {person.get('stage')}\n"
        f"Mandate: {person.get('mandate') or 'none noted'}\n"
        f"Next step: {person.get('next_step') or 'none noted'}\n"
    )

    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=500,
        system=DRAFT_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Profile:\n{profile}\nHistory:\n{history_text}\n\nInstruction: {instruction}",
        }],
    )
    draft = resp.content[0].text.strip()
    return f"Draft for {person.get('name') or person['email']} (review before sending):\n\n{draft}"
