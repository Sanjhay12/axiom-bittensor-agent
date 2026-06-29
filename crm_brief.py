"""
Phase 4 — Pre-call briefs. One-pager on a contact's background and history,
generated from their full interaction record, triggered before a meeting.
"""
from __future__ import annotations
import json
import os

from anthropic import AsyncAnthropic

import crm_store

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

BRIEF_SYSTEM_PROMPT = """You write a one-page pre-call brief for a fund manager about to meet a \
contact in their fundraising pipeline. You're given the contact's profile and full interaction \
history (each one sourced from a real email). Produce a brief covering: who they are, firm \
overview, prior history, what they care about, likely objections, what's been promised/sent, \
suggested agenda, suggested next ask, and recommended tone. Be concrete — pull from the actual \
interaction history, don't generalize. If a section has no supporting evidence, say "no data yet" \
rather than inventing detail. Write for Telegram: no markdown headers, bold the section labels.
"""


def _enrichment_text(person: dict) -> str:
    raw = person.get("enrichment")
    if not raw:
        return "none"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return "none"
    parts = []
    if (data.get("news") or {}).get("summary"):
        parts.append(f"News: {data['news']['summary']}")
    if (data.get("funding") or {}).get("summary"):
        parts.append(f"Funding: {data['funding']['summary']}")
    return " | ".join(parts) if parts else "none"


async def generate(query: str) -> str:
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
        f"Role: {person.get('role') or 'unknown'}\n"
        f"Relationship type: {person.get('relationship_type') or 'unknown'}\n"
        f"Mandate discussed: {person.get('mandate') or 'none noted'}\n"
        f"Stage: {person.get('stage')}\n"
        f"Next step: {person.get('next_step') or 'none noted'}\n"
        f"Notes: {person.get('notes') or 'none'}\n"
        f"External enrichment: {_enrichment_text(person)}\n"
    )

    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=900,
        system=BRIEF_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Profile:\n{profile}\nInteraction history:\n{history_text}"}],
    )
    return resp.content[0].text.strip()
