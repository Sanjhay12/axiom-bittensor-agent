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
    """Grouped by stage. Contacts with opportunities show per-product stages;
    contacts without show their contact-level stage."""
    import psycopg2.extras
    import store as _store

    # Fetch all opportunities with person + firm info
    with _store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT o.product, o.stage, o.deal_amount_usd, o.next_step,
                       p.name, p.email, f.name AS firm_name
                FROM crm_opportunities o
                JOIN crm_people p ON p.id = o.person_id
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                ORDER BY o.stage, p.name
            """)
            opps = cur.fetchall()

    # Contacts with no opportunities — use contact-level stage
    people = crm_store.list_people_by_stage_detailed()
    with _store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT DISTINCT person_id FROM crm_opportunities")
            opp_person_ids = {r["person_id"] for r in cur.fetchall()}

    no_opp_people = [p for p in people if p.get("id") not in opp_person_ids] if opps else people

    if not opps and not no_opp_people:
        return "No contacts in the pipeline yet."

    lines = ["<b>Pipeline</b>"]

    if opps:
        lines.append("\n<b>By opportunity:</b>")
        by_stage: dict[str, list] = {}
        for o in opps:
            by_stage.setdefault(o["stage"], []).append(o)
        opp_total = sum(o.get("deal_amount_usd") or 0 for o in opps)
        lines.append(f"Total tracked: {_fmt_amount(opp_total)} across {len(opps)} opportunities\n")
        for stage, group in by_stage.items():
            stage_total = sum(o.get("deal_amount_usd") or 0 for o in group)
            lines.append(f"<b>{stage}</b> ({len(group)}, {_fmt_amount(stage_total)})")
            for o in group:
                name = o.get("name") or o["email"]
                firm = f" ({o['firm_name']})" if o.get("firm_name") else ""
                line = f"  {name}{firm} — {o['product']} — {_fmt_amount(o.get('deal_amount_usd'))}"
                if o.get("next_step"):
                    line += f"\n    Next: {o['next_step']}"
                lines.append(line)
            lines.append("")

    if no_opp_people:
        lines.append("\n<b>Contacts (no opportunities logged):</b>")
        by_stage2: dict[str, list] = {}
        for p in no_opp_people:
            by_stage2.setdefault(p["stage"] or "New", []).append(p)
        for stage, group in by_stage2.items():
            lines.append(f"<b>{stage}</b> ({len(group)})")
            for p in group:
                name = p.get("name") or p["email"]
                firm = f" ({p['firm_name']})" if p.get("firm_name") else ""
                lines.append(f"  {name}{firm}")
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


UPDATE_SYSTEM_PROMPT = """You extract structured contact updates from a freeform note written by a fund manager.
Return ONLY valid JSON with any of these keys that are mentioned or clearly implied — omit keys with no supporting evidence:

{
  "phone": "string — phone number",
  "role": "string — their title or role at the firm",
  "mandate": "string — fund or deal being discussed",
  "deal_amount_usd": "number — dollar amount in plain USD (e.g. $5M → 5000000)",
  "contact_channel": "email | phone | video_call | in_person | other",
  "investor_type": "family_office | allocator | consultant | advisor | prospect | client | other",
  "how_met": "string",
  "introduced_by": "string",
  "personal_notes": "string — personal details: family, hobbies, travel, preferences",
  "warmth": "hot | warm | cooling | dormant",
  "communication_style": "string — how they prefer to be contacted",
  "cares_about": "string — investment themes or criteria they've expressed",
  "passed_on": "string — what they've previously declined",
  "revisit_later": "string — what they asked to follow up on",
  "liked_products": "string — strategies or products they expressed interest in",
  "objections": "string — hesitations or objections raised",
  "move_forward_conditions": "string — what needs to happen before they commit",
  "notes": "string — any other business context",
  "next_step": "string",
  "stage": "New | Contacted | Engaged | Intro made | Materials sent | Call scheduled | Diligence | Soft circled | Committed | Passed | Dormant"
}
"""


NATURAL_UPDATE_PROMPT = """You decide whether a message is requesting a CRM update (stage, next step, notes, etc.) or is a question/query.

Return ONLY valid JSON — no markdown, no commentary.

If it IS an update request:
{
  "is_update": true,
  "contact": "the exact name, email, or firm the user mentioned",
  "updates": {
    "stage": "New | Contacted | Engaged | Intro made | Materials sent | Call scheduled | Diligence | Soft circled | Committed | Passed | Dormant",
    "next_step": "string",
    "notes": "string",
    "phone": "string",
    "role": "string",
    "mandate": "string",
    "deal_amount_usd": "number — plain USD (e.g. $5M → 5000000)",
    "contact_channel": "email | phone | video_call | in_person | other",
    "warmth": "hot | warm | cooling | dormant",
    "investor_type": "family_office | allocator | consultant | advisor | prospect | client | other",
    "how_met": "string",
    "introduced_by": "string",
    "personal_notes": "string",
    "communication_style": "string",
    "cares_about": "string",
    "passed_on": "string",
    "revisit_later": "string",
    "liked_products": "string",
    "objections": "string",
    "move_forward_conditions": "string"
  }
}

Only include fields that are clearly being set. If the user says "next step for tomorrow" use "Follow up tomorrow" as the value. If the stage is "in diligence" map it to "Diligence".

If it is NOT an update request (it's a question or read-only command), return:
{"is_update": false}
"""


async def try_natural_update(note: str) -> str | None:
    """Detect and execute natural-language update requests. Returns None if not an update."""
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=350,
        system=NATURAL_UPDATE_PROMPT,
        messages=[{"role": "user", "content": note}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not data.get("is_update"):
        return None

    contact_query = (data.get("contact") or "").strip()
    updates = {k: v for k, v in (data.get("updates") or {}).items() if v is not None and v != ""}
    if not contact_query or not updates:
        return None

    # Try individual match first, then firm-wide
    person = crm_store.find_person(contact_query)
    people = [person] if person else crm_store.find_people_by_firm(contact_query)

    if not people:
        return f"No contact or firm matching '{contact_query}'."

    import time as _time
    import store as _store

    updated_names = []
    for p in people:
        fields_sql = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [int(_time.time()), p["id"]]
        with _store.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE crm_people SET {fields_sql}, updated_at = %s WHERE id = %s",
                    values,
                )
        updated_names.append(p.get("name") or p["email"])

    summary = "\n".join(f"  {k}: {v}" for k, v in updates.items())
    names = ", ".join(updated_names)
    return f"Updated {names}:\n{summary}"


async def apply_update(person_id: int, freeform: str) -> dict:
    """Use Claude to extract structured fields from freeform text and apply them to the contact."""
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=400,
        system=UPDATE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": freeform}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    updates = json.loads(text)
    if updates:
        import time as _time
        import store as _store
        import psycopg2
        fields = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [int(_time.time()), person_id]
        with _store.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE crm_people SET {fields}, updated_at = %s WHERE id = %s",
                    values,
                )
    return updates


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
