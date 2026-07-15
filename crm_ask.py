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
relationship type, contact-level mandate, pipeline stage, days since last touch, next step, notes, \
phone, contact channel, investor type, how/who they were introduced by, warmth, communication style, \
what they care about, what they've passed on, what to revisit later, liked products, objections (both \
a free-form summary and a bucketed profile with resolved/unresolved state), move-forward conditions, \
personal notes, whether they're manually flagged high priority, any pending (unconfirmed) stage change, \
external enrichment (LinkedIn, recent news, funding history — may be empty if not yet researched), a \
summary of the most recent interaction, and an "opportunities" array — a contact can be discussing \
several distinct products/deals at once, each with its own product name, stage, deal amount, next \
step, and objections, separate from the contact's own stage — plus a question from the fund manager \
who owns this data.

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
        enrichment = r.get("enrichment")
        try:
            enrichment = json.loads(enrichment) if isinstance(enrichment, str) else enrichment
        except (json.JSONDecodeError, TypeError):
            enrichment = None
        compact.append({
            "name": r.get("name"), "email": r.get("email"), "firm": r.get("firm_name"),
            "role": r.get("role"), "relationship_type": r.get("relationship_type"),
            "mandate": r.get("mandate"), "stage": r.get("stage"),
            "pending_stage": r.get("pending_stage"),
            "days_since_last_touch": days, "next_step": r.get("next_step"),
            "notes": r.get("notes"), "importance": r.get("importance"),
            "phone": r.get("phone"), "contact_channel": r.get("contact_channel"),
            "investor_type": r.get("investor_type"), "how_met": r.get("how_met"),
            "introduced_by": r.get("introduced_by"), "warmth": r.get("warmth"),
            "communication_style": r.get("communication_style"),
            "cares_about": r.get("cares_about"), "passed_on": r.get("passed_on"),
            "revisit_later": r.get("revisit_later"), "liked_products": r.get("liked_products"),
            "objections": r.get("objections"), "objection_profile": r.get("objection_profile"),
            "objection_resolutions": r.get("objection_resolutions"),
            "move_forward_conditions": r.get("move_forward_conditions"),
            "personal_notes": r.get("personal_notes"),
            "high_priority": bool(r.get("manual_priority")),
            "deal_amount_usd": r.get("deal_amount_usd"),
            "enrichment": enrichment,
            "last_interaction_summary": r.get("last_summary"),
            "opportunities": r.get("opportunities") or [],
        })
    return json.dumps(compact)


async def answer(question: str) -> str:
    context = _build_context()
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=700,
        system=ASK_SYSTEM_PROMPT + crm_store.directives_prompt_block(),
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
                       p.name, p.email, COALESCE(pf.name, ff.name) AS firm_name
                FROM crm_opportunities o
                LEFT JOIN crm_people p ON p.id = o.person_id
                LEFT JOIN crm_firms pf ON pf.id = p.firm_id
                LEFT JOIN crm_firms ff ON ff.id = o.firm_id
                ORDER BY o.stage, COALESCE(p.name, ff.name)
            """)
            opps = cur.fetchall()

    # Contacts with no opportunities — use contact-level stage
    people = crm_store.list_people_by_stage_detailed()
    with _store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT DISTINCT person_id FROM crm_opportunities WHERE person_id IS NOT NULL")
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
                if o.get("name") or o.get("email"):
                    name = o.get("name") or o["email"]
                    firm = f" ({o['firm_name']})" if o.get("firm_name") else ""
                else:
                    name = o.get("firm_name") or "Unknown firm"
                    firm = ""
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

This CRM keeps two separate things per contact: the contact's own profile (stage, phone, personal \
notes, general relationship context — one record), and their opportunities (one row PER product/deal \
being discussed with them — a contact can have several). Get this distinction right: if the message is \
about a specific product, fund, or deal (e.g. "new opportunity for product B", "put them in diligence \
for Fund II", "raise the ticket size on the credit strategy to $5m"), that's an opportunity update, NOT \
a generic contact note — losing the product-level tracking defeats the point of having it. If the \
message is about the contact generally (how they prefer to be reached, personal details, a stage change \
with no product named, etc.) with no specific product/deal mentioned, that's a contact update.

Return ONLY valid JSON — no markdown, no commentary.

If it IS a contact-level update request (no specific product/deal named):
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

If it IS an opportunity update (a specific product/fund/deal is named or clearly implied):
{
  "is_update": true,
  "contact": "the exact name, email, or firm the user mentioned",
  "opportunity": {
    "product": "string — the product/fund/deal name, required",
    "stage": "New | Contacted | Engaged | Intro made | Materials sent | Call scheduled | Diligence | Soft circled | Committed | Passed | Dormant",
    "deal_amount_usd": "number — plain USD (e.g. $5M → 5000000)",
    "next_step": "string",
    "mandate": "string",
    "notes": "string"
  }
}

Only include fields that are clearly being set. If the user says "next step for tomorrow" use "Follow up tomorrow" as the value. If the stage is "in diligence" map it to "Diligence". Never invent a product name — if it's clearly opportunity-shaped but no product is named or inferable, fall back to a contact-level update instead of guessing.

If it is NOT an update request (it's a question or read-only command), return:
{"is_update": false}
"""


BULK_OPPORTUNITY_PROMPT = """Decide whether this message is asking to create or update the \
same product/opportunity across MULTIPLE named companies/firms at once — e.g. "create \
opportunities for Fund X at Firm A, Firm B, and Firm C" or a note that just lists a product \
followed by a list of firm names. This only matches when TWO OR MORE distinct firm names are \
given for the same product — a single firm/contact is handled elsewhere, so return is_bulk: \
false for that case.

Return ONLY valid JSON — no markdown, no commentary.

If it IS a multi-firm opportunity request:
{
  "is_bulk": true,
  "product": "string — the product/fund/deal name",
  "firms": ["Firm A", "Firm B", "..."],
  "stage": "New | Contacted | Engaged | Intro made | Materials sent | Call scheduled | Diligence | Soft circled | Committed | Passed | Dormant | null"
}

List every firm name exactly as the user wrote it — don't expand abbreviations or guess a fuller \
name, the firm lookup handles fuzzy matching on its own.

Otherwise return: {"is_bulk": false}
"""


async def try_bulk_opportunity_update(note: str) -> str | None:
    """Detect and execute a single product applied across many named firms at once (e.g. Joe's
    "create opportunities for Nebari: Eagle Advisors, Allstate, ..." style request). Firms with
    no record on file get a bare-bones account created on the spot (get_or_create_firm) rather
    than being skipped — a fund manager naming a firm in a bulk raise list wants it tracked now,
    not blocked on sending a contact first; get_or_create_firm's own duplicate-matching (plus the
    duplicate-firm-flag check in crm_agent) keeps this from silently fragmenting into near-dupes.
    Returns None if this isn't a multi-firm request, so the caller falls through to the
    single-contact update path."""
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=400,
        system=BULK_OPPORTUNITY_PROMPT,
        messages=[{"role": "user", "content": note}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not data.get("is_bulk"):
        return None

    product = (data.get("product") or "").strip()
    firms = [f.strip() for f in (data.get("firms") or []) if f and f.strip()]
    if not product or len(firms) < 2:
        return None

    stage = data.get("stage")
    created, new_accounts = [], []
    for firm_name in firms:
        firm_id, is_new = crm_store.get_or_create_firm(firm_name)
        if firm_id is None:
            continue
        crm_store.upsert_opportunity(None, product, stage=stage, firm_id=firm_id)
        created.append(firm_name)
        if is_new:
            new_accounts.append(firm_name)

    lines = [f"Created '{product}' opportunity ({stage or 'New'}) for {len(created)} firm(s):"]
    lines += [f"  - {n}" for n in created] if created else ["  (none)"]
    if new_accounts:
        lines.append("")
        lines.append(
            f"No prior record for {len(new_accounts)} of these, so I created bare-bones accounts: "
            f"{', '.join(new_accounts)}. Send a contact name/email when you have one and I'll fill them in."
        )
    return "\n".join(lines)


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
    if not contact_query:
        return None

    # Try individual match first, then firm-wide
    person = crm_store.find_person(contact_query)
    people = [person] if person else crm_store.find_people_by_firm(contact_query)

    opp = data.get("opportunity") or {}
    product = (opp.get("product") or "").strip()

    if not people:
        # No contact/firm on file. If this carries an opportunity, create the account and log it
        # at the firm level rather than refusing — lack of contact info must not block a deal.
        if product:
            firm_id, is_new = crm_store.get_or_create_firm(contact_query)
            if firm_id is not None:
                opp_fields = {k: v for k, v in opp.items() if k != "product" and v not in (None, "")}
                crm_store.upsert_opportunity(
                    None, product, stage=opp_fields.get("stage"),
                    deal_amount_usd=opp_fields.get("deal_amount_usd"),
                    next_step=opp_fields.get("next_step"), mandate=opp_fields.get("mandate"),
                    notes=opp_fields.get("notes"), firm_id=firm_id,
                )
                if is_new:
                    import asyncio as _asyncio
                    import crm_enrich
                    _asyncio.create_task(crm_enrich.enrich_firm(firm_id, contact_query))
                return (f"Created account <b>{contact_query}</b> and logged the <b>{product}</b> "
                        + ("opportunity — researching public info." if is_new else "opportunity."))
        return f"No contact or firm matching '{contact_query}'."

    if product:
        opp_fields = {k: v for k, v in opp.items() if k != "product" and v is not None and v != ""}
        summaries = []
        for p in people:
            crm_store.upsert_opportunity(
                p["id"], product,
                stage=opp_fields.get("stage"),
                deal_amount_usd=opp_fields.get("deal_amount_usd"),
                next_step=opp_fields.get("next_step"),
                mandate=opp_fields.get("mandate"),
                notes=opp_fields.get("notes"),
            )
            summaries.append(p.get("name") or p["email"])
        detail = "\n".join(f"  {k}: {v}" for k, v in opp_fields.items())
        return f"Logged opportunity '{product}' for {', '.join(summaries)}:\n{detail}" if detail else f"Logged opportunity '{product}' for {', '.join(summaries)}."

    updates = {k: v for k, v in (data.get("updates") or {}).items() if v is not None and v != ""}
    if not updates:
        return None

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


RESOLVE_FLAGS_PROMPT = """You are deciding the outcome of pending "possible duplicate person" flags \
in a CRM, based on a fund manager's reply email. You're given a JSON list of pending flags (each with \
a flag_id and two people — a_name/a_firm/a_email and b_name/b_firm/b_email) plus the manager's reply.

For each flag the reply clearly addresses, decide:
- "merge" — the two records are the same person and should be combined
- "dismiss" — they are genuinely different people and should be left as separate contacts

Return ONLY valid JSON: a list of objects with "flag_id" and "decision" (one of "merge"/"dismiss"). \
Omit any flag the reply doesn't address — don't guess. If the reply clearly applies to every listed \
flag at once (e.g. "merge them all", "those are all different people"), include every flag_id.

Flags:
{flags_json}
"""


async def try_resolve_name_flags(note: str, flags: list[dict]) -> str | None:
    """flags: pending crm_name_flags rows (flag_id, a_id/a_name/a_firm/a_email, b_id/b_name/b_firm/b_email).
    b is always the pre-existing record, a is the one just seen/imported — merging keeps b's id
    since it's the more established record. Returns a confirmation string per resolved flag, or
    None if the reply didn't clearly address any of them (caller should fall through to normal
    note handling)."""
    if not flags:
        return None
    flags_for_prompt = [
        {
            "flag_id": f["flag_id"],
            "a_name": f["a_name"], "a_firm": f.get("a_firm"), "a_email": f["a_email"],
            "b_name": f["b_name"], "b_firm": f.get("b_firm"), "b_email": f["b_email"],
        }
        for f in flags
    ]
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=400,
        system=RESOLVE_FLAGS_PROMPT.format(flags_json=json.dumps(flags_for_prompt)),
        messages=[{"role": "user", "content": note}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        decisions = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not decisions:
        return None

    by_id = {f["flag_id"]: f for f in flags}
    results = []
    for d in decisions:
        f = by_id.get(d.get("flag_id"))
        decision = d.get("decision")
        if not f or decision not in ("merge", "dismiss"):
            continue
        if decision == "merge":
            crm_store.merge_people(f["b_id"], f["a_id"])
            results.append(f"Merged {f['a_name']} ({f.get('a_firm') or 'unknown firm'}) into {f['b_name']} ({f.get('b_firm') or 'unknown firm'}).")
        else:
            crm_store.resolve_flag(f["flag_id"], "dismissed")
            results.append(f"Kept {f['a_name']} and {f['b_name']} as separate contacts.")

    return "\n".join(results) if results else None


RESOLVE_FIRM_FLAGS_PROMPT = """You are deciding the outcome of pending "possible duplicate firm" flags \
in a CRM, based on a fund manager's reply email. You're given a JSON list of pending flags (each with a \
flag_id and two firm names — a_name and b_name) plus the manager's reply.

For each flag the reply clearly addresses, decide:
- "merge" — the two names refer to the same company and should be combined
- "dismiss" — they are genuinely different companies and should stay separate

Return ONLY valid JSON: a list of objects with "flag_id" and "decision" (one of "merge"/"dismiss"). \
Omit any flag the reply doesn't address — don't guess. If the reply clearly applies to every listed \
flag at once (e.g. "merge them all", "those are different companies"), include every flag_id.

Flags:
{flags_json}
"""


async def try_resolve_firm_flags(note: str, flags: list[dict]) -> str | None:
    """flags: pending crm_firm_flags rows (flag_id, a_id/a_name, b_id/b_name). b is always the
    pre-existing firm, a is the one just created — merging keeps b's id since it's the more
    established record. Returns a confirmation string per resolved flag, or None if the reply
    didn't clearly address any of them (caller should fall through to normal note handling)."""
    if not flags:
        return None
    flags_for_prompt = [
        {"flag_id": f["flag_id"], "a_name": f["a_name"], "b_name": f["b_name"]}
        for f in flags
    ]
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=400,
        system=RESOLVE_FIRM_FLAGS_PROMPT.format(flags_json=json.dumps(flags_for_prompt)),
        messages=[{"role": "user", "content": note}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        decisions = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not decisions:
        return None

    by_id = {f["flag_id"]: f for f in flags}
    results = []
    for d in decisions:
        f = by_id.get(d.get("flag_id"))
        decision = d.get("decision")
        if not f or decision not in ("merge", "dismiss"):
            continue
        if decision == "merge":
            crm_store.merge_firms(f["b_id"], f["a_id"])
            results.append(f"Merged '{f['a_name']}' into '{f['b_name']}'.")
        else:
            crm_store.resolve_firm_flag(f["flag_id"], "dismissed")
            results.append(f"Kept '{f['a_name']}' and '{f['b_name']}' as separate companies.")

    return "\n".join(results) if results else None


MERGE_COMMAND_PROMPT = """Decide whether this message is asking to merge two CRM contacts (they're \
the same person) or mark two contacts as different people — as a standalone request, not a reply to \
a specific flag email. Only match this if TWO distinct people are clearly named.

Return ONLY valid JSON.

If it's a merge/dismiss request naming two people:
{
  "action": "merge" or "dismiss",
  "person_a": {"name": "string", "firm": "string or null", "email": "string or null"},
  "person_b": {"name": "string", "firm": "string or null", "email": "string or null"}
}

Otherwise return: {"action": null}
"""


async def try_merge_command(note: str) -> str | None:
    """Standalone free-form merge/dismiss — works any time, not just as a reply to an
    auto-generated flag. Only acts when each named person resolves to exactly one
    contact; a merge is destructive (deletes a row) so an ambiguous match asks for
    clarification instead of guessing."""
    resp = await claude.messages.create(
        model=MODEL, max_tokens=300, system=MERGE_COMMAND_PROMPT,
        messages=[{"role": "user", "content": note}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not data.get("action"):
        return None

    def _resolve(ref: dict) -> list[dict]:
        email = (ref.get("email") or "").strip()
        if email:
            p = crm_store.find_person(email)
            return [p] if p else []
        name = (ref.get("name") or "").strip()
        if not name:
            return []
        candidates = crm_store.find_people_by_name(name)
        firm = (ref.get("firm") or "").strip().lower()
        if firm and len(candidates) > 1:
            narrowed = [c for c in candidates if firm in (c.get("firm_name") or "").lower()]
            if narrowed:
                candidates = narrowed
        return candidates

    a_candidates = _resolve(data.get("person_a") or {})
    b_candidates = _resolve(data.get("person_b") or {})

    if len(a_candidates) != 1 or len(b_candidates) != 1:
        def _describe(cands, ref):
            label = ref.get("name") or "that contact"
            if not cands:
                return f"couldn't find anyone matching '{label}'"
            options = "; ".join(f"{c['name']} — {c.get('firm_name') or 'unknown firm'} ({c['email']})" for c in cands)
            return f"multiple matches for '{label}': {options}"
        problems = []
        if len(a_candidates) != 1:
            problems.append(_describe(a_candidates, data.get("person_a") or {}))
        if len(b_candidates) != 1:
            problems.append(_describe(b_candidates, data.get("person_b") or {}))
        return "Couldn't resolve that — " + "; ".join(problems) + ". Try including an email address for each person."

    a, b = a_candidates[0], b_candidates[0]
    if a["id"] == b["id"]:
        return "Those are already the same contact."

    if data["action"] == "merge":
        keep, remove = (a, b) if a["id"] < b["id"] else (b, a)
        crm_store.merge_people(keep["id"], remove["id"])
        return f"Merged {remove['name']} ({remove.get('firm_name') or 'unknown firm'}) into {keep['name']} ({keep.get('firm_name') or 'unknown firm'})."

    flag_id = crm_store.create_name_flag(a["id"], b["id"], None)
    crm_store.resolve_flag(flag_id, "dismissed")
    return (
        f"Got it — {a['name']} ({a.get('firm_name') or 'unknown firm'}) and "
        f"{b['name']} ({b.get('firm_name') or 'unknown firm'}) are different people. Won't flag that pair again."
    )


OBJECTION_COMMAND_PROMPT = f"""Decide whether this message is asking to resolve or erase ONE specific \
objection on a CRM contact (or on one of their named products/deals). Only match if a specific \
objection concern is identifiable — not a generic "clear their notes" or unrelated update request.

"resolve" means the objection has been handled/addressed but should stay on record (e.g. "Jane's fee \
concern is resolved", "he's fine with the liquidity terms now", "no longer worried about headline risk").
"erase" means the objection was logged in error and should be deleted outright (e.g. "delete that fee \
objection, that was a mistake", "remove the liquidity concern, never happened").

Map the concern described to exactly one of these known objection buckets:
{json.dumps(crm_store.OBJECTION_LABELS)}

Return ONLY valid JSON:
{{
  "action": "resolve" or "erase" or null,
  "contact": "the exact name, email, or firm mentioned",
  "product": "the specific product/deal name if one is named (objection is opportunity-level), else null",
  "bucket": "one of the known bucket keys above that best matches the concern, or null if it doesn't clearly map to any of them"
}}

If this isn't a resolve/erase request for a specific objection, return: {{"action": null}}
"""


async def try_objection_command(note: str) -> str | None:
    """Detect and execute a resolve/erase request for one objection bucket — contact-level by
    default, or opportunity-level if a specific product/deal is named. Resolving keeps the
    bucket in objection_profile (marked handled, still visible in briefs/history); erasing
    removes it entirely, for objections logged in error. Returns None if this isn't an
    objection resolve/erase request, so the caller falls through to normal note handling."""
    resp = await claude.messages.create(
        model=MODEL, max_tokens=300, system=OBJECTION_COMMAND_PROMPT,
        messages=[{"role": "user", "content": note}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    action = data.get("action")
    if action not in ("resolve", "erase"):
        return None

    contact_query = (data.get("contact") or "").strip()
    if not contact_query:
        return None
    person = crm_store.find_person(contact_query)
    if not person:
        return f"No contact matching '{contact_query}'."
    display_name = person.get("name") or person["email"]

    bucket = (data.get("bucket") or "").strip()
    if bucket not in crm_store.OBJECTION_LABELS:
        return (
            f"Couldn't tell which objection you meant for {display_name} — try naming it more "
            f"specifically (e.g. fee, liquidity, timing)."
        )
    label = crm_store.OBJECTION_LABELS[bucket]
    fn = crm_store.resolve_objection if action == "resolve" else crm_store.erase_objection
    verb = "Resolved" if action == "resolve" else "Erased"

    product = (data.get("product") or "").strip()
    if product:
        opps = crm_store.list_opportunities(person["id"])
        match = next((o for o in opps if product.lower() in o["product"].lower()), None)
        if not match:
            have = ", ".join(o["product"] for o in opps) or "none logged"
            return f"No opportunity matching '{product}' for {display_name}. They have: {have}."
        result = fn(bucket, opportunity_id=match["id"])
        if result is None:
            return f"{display_name} has no '{label}' objection logged on {match['product']}."
        return f"{verb} the {label} objection on {match['product']} for {display_name}."

    result = fn(bucket, person_id=person["id"])
    if result is None:
        return f"{display_name} has no '{label}' objection logged."
    return f"{verb} the {label} objection for {display_name}."


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


ACTION_COMMAND_PROMPT = """Decide whether this message is asking to perform ONE of these specific \
actions on a single named CRM contact. Only match if the message clearly requests one of these —
general questions, relationship notes, or field updates (stage, phone, notes, etc.) are handled
elsewhere and should return null here.

- "mark_done": the contact's current next-step/task is complete and should be cleared (e.g. "mark
  Jane's next step done", "she already followed up, clear that task").
- "set_priority": always flag this contact in the daily digest (e.g. "flag Jane as high priority",
  "make sure I never miss her").
- "unset_priority": remove that flag (e.g. "Jane doesn't need to be high priority anymore").
- "confirm_stage": accept a pending pipeline stage change (e.g. "yes, confirm Jane's stage move",
  "that's right, she's in diligence now").
- "reject_stage": dismiss a pending pipeline stage change (e.g. "no, don't change her stage").
- "score_query": asking specifically for a contact's numeric LP score / signal breakdown (e.g.
  "what's Jane's score", "how warm is Jane on paper").
- "draft_request": asking to draft/write a reply or follow-up email to ONE specific named contact
  (e.g. "draft a follow-up to Jane about the fund", "write her a check-in note"). The recipient is
  a real person/firm you can name — and may be named MID-SENTENCE ("draft an outreach email TO
  Voice following up on the Nebari conversation…"): pull out just that name/firm as "contact" and
  put everything else (the ask, the talking points, the fund details) into "instruction", however
  long. This is NOT for roadshow/trip outreach: if the email is for meeting investors on a trip
  through one or more cities — even when it's phrased "draft an email for a roadshow..." — use
  "roadshow" with wants_drafts true instead, and leave "contact" null.
- "enrich_request": asking to research/look up a contact or a fund/firm's background — funding
  history, recent news, LinkedIn. "contact" here can be a fund/firm name that has NO contact or
  even a firm on file yet — e.g. "find funding history for Acme Capital" or "research Meridian
  Partners" should still match this even if Acme/Meridian has never come up before; the research
  gets saved so it's there next time. (e.g. "look up Jane's firm", "research Jane's background").
- "brief_request": asking to prepare for an upcoming call/meeting, OR asking for a brief/overview
  on a fund/firm that may not have any contact on file yet — a prospective LP's fund can still get
  a cold research-based brief (e.g. "prep me for my call with Jane", "brief me on Acme Capital").
- "roadshow": planning which investors to meet in a specific city/area or on an upcoming trip —
  NOT about one named contact (e.g. "I'm heading to LA with Nebari, who should I meet?", "who
  should I see when I'm in New York?", "plan my Boston trip"). Put the city in "city" and, if a
  fund/product is named, put it in "product". If SEVERAL cities are named (e.g. "a roadshow through
  Los Angeles and San Francisco"), put them all in "city" joined with " and ". Leave "contact"
  null. Set "wants_drafts" true if they're asking for the outreach/meeting EMAILS to be drafted —
  including phrasings like "draft an email for a roadshow through LA and SF", "draft the emails for
  my LA roadshow", "write outreach notes for the SF trip"; a plain "who should I meet" is false.
- "status_report": asking for an overall fundraising STATUS / progress summary to share with his
  manager or a fund — where the whole raise stands, not one contact (e.g. "give me a status update
  for the fund", "how's the raise going", "something I can send my manager on where we're at").
  Leave "contact" null.
- "set_location": stating or tagging where a contact or firm is BASED / located (e.g. "Fairbridge
  is based in Los Angeles", "mark Robert O'Connor as New York", "Acorn is in Chicago"). Put the
  firm or person in "contact" and the city in "location".

Return ONLY valid JSON:
{
  "action": "mark_done" | "set_priority" | "unset_priority" | "confirm_stage" | "reject_stage" | "score_query" | "draft_request" | "enrich_request" | "brief_request" | "roadshow" | "status_report" | "set_location" | null,
  "contact": "the exact name, email, or firm mentioned (null for roadshow / status_report)",
  "product": "for brief_request or roadshow — a specific product/deal name if mentioned, else null",
  "instruction": "for draft_request only — what the draft should say, else null",
  "city": "for roadshow only — the city/area to plan around, else null",
  "wants_drafts": "for roadshow only — true if they want the outreach emails drafted, else false",
  "location": "for set_location only — the city where the contact/firm is based, else null"
}

If none of these actions apply, return {"action": null}.
"""


async def try_action_command(note: str) -> dict | None:
    """Detect one of a small set of named CRM actions (mark done, priority flag, confirm/reject
    pending stage, score lookup, draft request, enrich request) expressed in free-form language
    rather than the exact command syntax. Unlike the other try_* helpers here, this returns the
    classified action + params rather than executing it — the caller (crm_agent.py) dispatches,
    since some actions (brief requests) need msg-level context this module doesn't have. Returns
    None if this isn't one of these actions."""
    resp = await claude.messages.create(
        model=MODEL, max_tokens=250, system=ACTION_COMMAND_PROMPT,
        messages=[{"role": "user", "content": note}],
    )
    text = resp.content[0].text.strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not data.get("action"):
        return None
    # roadshow / status_report are not contact-scoped; roadshow still needs a city.
    if data["action"] in ("roadshow", "status_report"):
        if data["action"] == "roadshow" and not (data.get("city") or "").strip():
            return None
        return data
    if not (data.get("contact") or "").strip():
        return None
    return data


DECOMPOSE_PROMPT = """You split a fund manager's email to his CRM assistant into the DISTINCT, separately-actionable requests it contains.

MOST emails are a SINGLE request — return it as one item. Only split when the email clearly asks for TWO OR MORE DIFFERENT deliverables, for example:
- "give me today's to-do AND a general status report" -> two tasks
- "a target list for the LA roadshow and draft the outreach emails" -> two tasks
- "brief me on Acme and draft a follow-up to Jane" -> two tasks

Do NOT split a single request into steps, and do NOT split the parameters of one request (e.g. "a roadshow through LA and SF" is ONE trip, "update her stage and phone" is ONE update). When you DO split, REWRITE each task so it stands ALONE, repeating the shared context — names, firms, cities, products, dates — inside each task so it can be handled on its own.

Return ONLY a JSON array of task strings. One item if it's a single request. Max 5 items."""


async def decompose_tasks(note: str) -> list[str]:
    """Split a multi-request email into standalone task strings so each gets handled.
    Returns [note] unchanged for a single request (the common case) or on any failure —
    so single asks and code-change commands are never reworded, only genuine multi-asks
    are split. See crm_agent._reply_to_note, which runs each returned task through routing."""
    note = (note or "").strip()
    if not note:
        return [note]
    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=600, system=DECOMPOSE_PROMPT,
            messages=[{"role": "user", "content": note}],
        )
        text = resp.content[0].text.strip().strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        tasks = [t.strip() for t in json.loads(text) if isinstance(t, str) and t.strip()]
    except Exception as e:
        logger.warning(f"crm_ask: task decomposition failed, treating as single request: {e}")
        return [note]
    # Only trust a genuine multi-split; a 1-item (or empty) result falls back to the
    # original wording so single requests and code commands are never altered.
    return tasks[:5] if len(tasks) >= 2 else [note]
