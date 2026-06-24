"""
Phase 3 — Pipeline + Ask Layer. Natural-language queries over the relationship
memory ("who is warm for Nebari?"), plus pipeline stage breakdown.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

import crm_store

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

ASK_SYSTEM_PROMPT = """You are the relationship-intelligence layer of a private fundraising CRM \
("Cedar Ridge Inbox Agent"). You're given a JSON list of contacts — each with name, firm, role, \
relationship type, mandate, pipeline stage, days since last touch, next step, notes, and a summary \
of the most recent interaction — plus a question from the fund manager who owns this data.

Answer directly and concisely, citing the specific contacts and evidence (firm, last touch, what \
was said) that back up your answer. If nothing in the data supports an answer, say so plainly — \
never invent a contact, firm, or detail that isn't in the provided data. Write for Telegram: no \
markdown headers, light use of bold for names is fine.
"""


def _build_context() -> str:
    rows = crm_store.all_people_brief_context()
    now = int(datetime.now(timezone.utc).timestamp())
    compact = []
    for r in rows:
        days = int((now - r["last_touch_ts"]) / 86400) if r.get("last_touch_ts") else None
        compact.append({
            "name": r.get("name"), "email": r.get("email"), "firm": r.get("firm_name"),
            "role": r.get("role"), "relationship_type": r.get("relationship_type"),
            "mandate": r.get("mandate"), "stage": r.get("stage"),
            "days_since_last_touch": days, "next_step": r.get("next_step"),
            "notes": r.get("notes"), "importance": r.get("importance"),
            "last_interaction_summary": r.get("last_summary"),
        })
    return json.dumps(compact)


async def answer(question: str) -> str:
    context = _build_context()
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=700,
        system=ASK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Contacts:\n{context}\n\nQuestion: {question}"}],
    )
    return resp.content[0].text.strip()


def pipeline_summary() -> str:
    counts = crm_store.stage_counts()
    if not counts:
        return "No contacts in the pipeline yet."
    lines = ["<b>Pipeline</b>"]
    for row in counts:
        lines.append(f"{row['stage']}: {row['cnt']}")
    return "\n".join(lines)


def confirm_stage(query: str) -> str:
    person = crm_store.find_person(query)
    if not person:
        return f"No contact matching '{query}'."
    if not person.get("pending_stage"):
        return f"{person.get('name') or person['email']} has no pending stage change."
    new_stage = crm_store.confirm_pending_stage(person["id"])
    return f"Confirmed: {person.get('name') or person['email']} → {new_stage}"


def reject_stage(query: str) -> str:
    person = crm_store.find_person(query)
    if not person:
        return f"No contact matching '{query}'."
    crm_store.reject_pending_stage(person["id"])
    return f"Dismissed pending stage change for {person.get('name') or person['email']}."
