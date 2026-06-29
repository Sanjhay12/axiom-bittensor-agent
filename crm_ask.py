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


def _fmt_amount(amount: float | None) -> str:
    if not amount:
        return "amount unknown"
    return f"${amount:,.0f}"


def pipeline_summary() -> str:
    """Grouped by stage, with each contact's expected transaction size and next step —
    not just a count, since a count alone doesn't tell you what's actually at stake."""
    people = crm_store.list_people_by_stage_detailed()
    if not people:
        return "No contacts in the pipeline yet."

    by_stage: dict[str, list[dict]] = {}
    for p in people:
        by_stage.setdefault(p["stage"], []).append(p)

    lines = ["<b>Pipeline</b>"]
    total = sum(p.get("deal_amount_usd") or 0 for p in people)
    lines.append(f"Total tracked: {_fmt_amount(total)} across {len(people)} contacts\n")

    for stage, group in by_stage.items():
        stage_total = sum(p.get("deal_amount_usd") or 0 for p in group)
        lines.append(f"<b>{stage}</b> ({len(group)}, {_fmt_amount(stage_total)})")
        for p in group:
            name = p.get("name") or p["email"]
            firm = f" ({p['firm_name']})" if p.get("firm_name") else ""
            line = f"  {name}{firm} — {_fmt_amount(p.get('deal_amount_usd'))}"
            if p.get("next_step"):
                line += f"\n    Next: {p['next_step']}"
            lines.append(line)
        lines.append("")

    return "\n".join(lines).strip()


def stage_filter(stage_query: str) -> str:
    """Answers "who is in diligence" / "who's in engaged" style queries directly from the
    database, rather than asking Claude to filter a JSON blob — deterministic and exact."""
    people = crm_store.list_people_in_stage(stage_query)
    if not people:
        return f"No one is currently in a stage matching '{stage_query}'."
    lines = [f"<b>In '{stage_query}'</b> ({len(people)})"]
    for p in people:
        name = p.get("name") or p["email"]
        firm = f" ({p['firm_name']})" if p.get("firm_name") else ""
        line = f"  {name}{firm} — {_fmt_amount(p.get('deal_amount_usd'))}"
        if p.get("next_step"):
            line += f"\n    Next: {p['next_step']}"
        lines.append(line)
    return "\n".join(lines)


def set_priority(query: str, value: bool) -> str:
    person = crm_store.find_person(query)
    if not person:
        return f"No contact matching '{query}'."
    crm_store.set_manual_priority(person["id"], value)
    name = person.get("name") or person["email"]
    return f"{name} {'marked' if value else 'unmarked'} as high priority."


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
