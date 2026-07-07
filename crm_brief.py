"""
Phase 4 — Pre-call briefs. One-pager on a contact's background and history,
generated from their full interaction record, triggered before a meeting.
"""
from __future__ import annotations
import json
import os

from anthropic import AsyncAnthropic

import crm_enrich
import crm_store

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

BRIEF_SYSTEM_PROMPT = """You write a pre-call brief for a fund manager at Cedar Ridge Capital who is about to meet a contact in their fundraising pipeline.

You're given the contact's full profile (including relationship history, what they care about, objections, personal notes) and their last 5 interactions sourced from real emails and notes.

Write the brief in exactly these 7 sections. Bold each section label. Be concrete — pull from the actual data, don't generalize. If a section has no supporting evidence, write "No data yet" rather than inventing detail.

<b>1. Who They Are</b>
Name, role, firm, firm type, how they invest (mandate, fund size if known), how Joe met them / who introduced them.

<b>2. Last 5 Interactions</b>
Chronological list. For each: date (if known), direction (inbound/outbound), and one sentence on what happened or was said.

<b>3. What They Care About</b>
Investment themes, criteria, preferences they've expressed. What gets them interested.

<b>4. What They've Passed On / Previously Rejected</b>
Strategies, deals, or products they've declined in the past. Don't pitch these again.

<b>5. Objections & Resistance</b>
Specific hesitations, concerns, or blockers they've raised. What's standing between them and moving forward.

<b>6. Personal Context</b>
Anything personal worth remembering: family, hobbies, travel, communication style preferences, what makes them tick. Use to build rapport.

<b>7. Suggested Angle for This Call</b>
Based on everything above: what's the most compelling entry point for this specific conversation? What should Joe lead with, avoid, and ask? What's the one thing that could move this forward?

Write for email — clean plain text, no markdown symbols beyond the bold labels already specified.
"""

PRODUCT_BRIEF_SYSTEM_PROMPT = """You write a pre-call brief for a fund manager at Cedar Ridge Capital, \
focused on ONE specific product/deal they're discussing with a contact — not the general relationship.

You're given the contact's general profile (for rapport context only), the specific opportunity's \
details (stage, deal amount, next step, notes, objections raised about THIS product specifically), \
and their last 5 interactions.

Write the brief in exactly these 5 sections. Bold each section label. Be concrete — pull from the \
actual data, don't generalize. If a section has no supporting evidence, write "No data yet".

<b>1. Deal Status</b>
Product name, pipeline stage, deal size, current next step.

<b>2. Objections on This Deal</b>
Specific concerns or hesitations raised about THIS product — not general objections about other deals.

<b>3. Relevant History</b>
What's been discussed about this specific product across recent interactions.

<b>4. Who They Are (for rapport)</b>
Brief contact context: role, firm, communication style, personal notes — kept short, this section is secondary.

<b>5. Suggested Angle for This Call</b>
Given the deal's current stage and objections, what's the most compelling next move? What should Joe lead with, avoid, and ask to move THIS specific deal forward?

Write for email — clean plain text, no markdown symbols beyond the bold labels already specified.
"""

COLD_FUND_BRIEF_PROMPT = """You write a pre-call brief for a fund manager at Cedar Ridge Capital \
about a fund/firm that has NO relationship history in the CRM yet — no prior contact, interaction, \
or pipeline data. You're given only external research (recent news, funding history) gathered from \
the web.

State plainly at the top that this fund has no prior contact or interaction on file — this is \
research-only, not relationship history.

Write the brief in exactly these 3 sections. Bold each section label. Be concrete — pull from the \
actual research, don't invent detail. If a section has no supporting evidence, write "No data found".

<b>1. Firm Overview</b>
What the firm/fund does, size/AUM if known, strategy, recent news.

<b>2. Funding / Track Record</b>
Funding history, fund performance, notable investors, if found.

<b>3. Suggested Approach</b>
Given this is a cold/first outreach with no relationship yet, what's a sensible opening angle based on what's known about the firm?

Write for email — clean plain text, no markdown symbols beyond the bold labels already specified.
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
    linkedin = data.get("linkedin") or {}
    if linkedin.get("url") or linkedin.get("linkedin_profile_url"):
        parts.append(f"LinkedIn: {linkedin.get('url') or linkedin.get('linkedin_profile_url')}")
    return " | ".join(parts) if parts else "none"


def _opt(label: str, val) -> str:
    return f"{label}: {val}\n" if val else ""


def _fmt_objection_profile(raw, raw_resolutions=None) -> str:
    if not raw:
        return ""
    try:
        profile = json.loads(raw) if isinstance(raw, str) else raw
        resolutions = (json.loads(raw_resolutions) if isinstance(raw_resolutions, str) else raw_resolutions) or {}
    except (json.JSONDecodeError, TypeError):
        return ""
    lines = [
        f"  {crm_store.OBJECTION_LABELS.get(k, k)}: {v}" + ("  (resolved)" if resolutions.get(k) else "")
        for k, v in profile.items() if v
    ]
    return "Objection profile:\n" + "\n".join(lines) + "\n" if lines else ""


async def generate(query: str, product: str | None = None) -> str:
    person = crm_store.find_person(query)
    if person:
        if product:
            opps = crm_store.list_opportunities(person["id"])
            match = next((o for o in opps if product.strip().lower() in o["product"].lower()), None)
            if not match:
                have = ", ".join(o["product"] for o in opps) or "none logged"
                return f"No opportunity matching '{product}' for {person.get('name') or person['email']}. They have: {have}."
            return await _opportunity_brief(person, match)
        return await _person_brief(person)

    people = crm_store.find_people_by_firm(query)
    if people:
        return await _firm_brief(query, people)

    # No contact and no firm with contacts on file — could still be a fund the manager
    # wants researched cold, before any relationship exists. Resolve/create the firm
    # record and brief off fresh web research rather than failing outright.
    firm_id, _ = crm_store.get_or_create_firm(query)
    if firm_id is None:
        return f"No contact or firm matching '{query}'."
    return await _cold_fund_brief(firm_id, query)


async def _opportunity_brief(person: dict, opp: dict) -> str:
    interactions = crm_store.list_interactions(person["id"])[:5]
    history_text = "\n".join(
        f"- [{i.get('direction', '?')}] {i.get('subject') or '(no subject)'}: {i.get('summary') or '(no summary)'}"
        for i in interactions
    ) or "No recorded interactions yet."

    opp_text = (
        f"Product: {opp['product']}\n"
        f"Stage: {opp.get('stage') or 'New'}\n"
        + (f"Deal amount: ${opp['deal_amount_usd']:,.0f}\n" if opp.get("deal_amount_usd") else "")
        + (f"Next step: {opp['next_step']}\n" if opp.get("next_step") else "")
        + (f"Notes: {opp['notes']}\n" if opp.get("notes") else "")
        + (f"Objections on this product: {opp['objections']}\n" if opp.get("objections") else "")
        + _fmt_objection_profile(opp.get("objection_profile"), opp.get("objection_resolutions"))
    )

    contact_text = (
        f"Name: {person.get('name') or person['email']}\n"
        f"Firm: {person.get('firm_name') or 'unknown'}\n"
        f"Role: {person.get('role') or 'unknown'}\n"
        + _opt("Communication style", person.get("communication_style"))
        + _opt("Personal notes", person.get("personal_notes"))
    )

    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=900,
        system=PRODUCT_BRIEF_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Opportunity:\n{opp_text}\nContact:\n{contact_text}\nLast 5 interactions:\n{history_text}",
        }],
    )
    return resp.content[0].text.strip()


async def _person_brief(person: dict) -> str:
    interactions = crm_store.list_interactions(person["id"])[:5]
    history_text = "\n".join(
        f"- [{i.get('direction', '?')}] {i.get('subject') or '(no subject)'}: {i.get('summary') or '(no summary)'}"
        for i in interactions
    ) or "No recorded interactions yet."

    # Opportunities
    opps = crm_store.list_opportunities(person["id"])
    opps_text = ""
    if opps:
        opps_text = "\n".join(
            f"  {o['product']} — {o['stage']}" + (f" — ${o['deal_amount_usd']:,.0f}" if o.get('deal_amount_usd') else "")
            for o in opps
        )

    profile = (
        f"Name: {person.get('name') or person['email']}\n"
        f"Email: {person.get('email') or 'unknown'}\n"
        f"Firm: {person.get('firm_name') or 'unknown'}\n"
        f"Role: {person.get('role') or 'unknown'}\n"
        + _opt("Investor type", person.get("investor_type"))
        + _opt("Relationship type", person.get("relationship_type"))
        + _opt("Mandate / focus", person.get("mandate"))
        + _opt("Pipeline stage", person.get("stage"))
        + _opt("Warmth", person.get("warmth"))
        + _opt("How Joe met them", person.get("how_met"))
        + _opt("Introduced by", person.get("introduced_by"))
        + _opt("What they care about", person.get("cares_about"))
        + _opt("Previously passed on", person.get("passed_on"))
        + _opt("Liked products / interest", person.get("liked_products"))
        + _opt("Objections raised (summary)", person.get("objections"))
        + _fmt_objection_profile(person.get("objection_profile"), person.get("objection_resolutions"))
        + _opt("Move-forward conditions", person.get("move_forward_conditions"))
        + _opt("Revisit later", person.get("revisit_later"))
        + _opt("Communication style", person.get("communication_style"))
        + _opt("Personal notes", person.get("personal_notes"))
        + _opt("Notes", person.get("notes"))
        + _opt("Next step", person.get("next_step"))
        + (f"Opportunities:\n{opps_text}\n" if opps_text else "")
        + f"External enrichment: {_enrichment_text(person)}\n"
    )

    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=BRIEF_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Profile:\n{profile}\nLast 5 interactions:\n{history_text}"}],
    )
    return resp.content[0].text.strip()


async def _firm_brief(firm_query: str, people: list[dict]) -> str:
    firm_name = people[0].get("firm_name") or firm_query
    contacts_text = ""
    all_interactions = []

    for p in people:
        interactions = crm_store.list_interactions(p["id"])
        for i in interactions:
            i["_person_name"] = p.get("name") or p["email"]
        all_interactions.extend(interactions)

        opps = crm_store.list_opportunities(p["id"])
        opps_text = ""
        if opps:
            opps_text = "  Opportunities:\n" + "\n".join(
                f"    {o['product']} — {o['stage']}" + (f" — ${o['deal_amount_usd']:,.0f}" if o.get('deal_amount_usd') else "")
                for o in opps
            ) + "\n"

        contacts_text += (
            f"\n{p.get('name') or p['email']} ({p.get('role') or 'unknown role'})\n"
            f"  Stage: {p.get('stage') or 'unknown'} | Warmth: {p.get('warmth') or 'unknown'}\n"
            + (f"  Cares about: {p['cares_about']}\n" if p.get("cares_about") else "")
            + (f"  Objections: {p['objections']}\n" if p.get("objections") else "")
            + (f"  Passed on: {p['passed_on']}\n" if p.get("passed_on") else "")
            + (f"  Personal: {p['personal_notes']}\n" if p.get("personal_notes") else "")
            + opps_text
        )

    # Sort all interactions by ts desc, take last 5 across all contacts at firm
    all_interactions.sort(key=lambda i: i.get("ts") or 0, reverse=True)
    recent = all_interactions[:5]
    history_text = "\n".join(
        f"- [{i.get('direction', '?')}] {i['_person_name']}: {i.get('summary') or i.get('subject') or '(no summary)'}"
        for i in recent
    ) or "No recorded interactions yet."

    prompt = f"Firm: {firm_name}\nContacts at firm:\n{contacts_text}\nLast 5 interactions (across all contacts):\n{history_text}"

    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=1400,
        system=BRIEF_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


async def _cold_fund_brief(firm_id: int, firm_name: str) -> str:
    """Brief for a fund/firm with no contact or interaction history at all — reuses
    saved research if this firm was already researched (via the enrich_request command
    or a prior cold brief), otherwise runs it fresh now."""
    firm = crm_store.get_firm(firm_id)
    raw = firm.get("enrichment") if firm else None
    try:
        data = (json.loads(raw) if isinstance(raw, str) else raw) or {} if raw else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not data:
        data = await crm_enrich.enrich_firm(firm_id, firm_name)

    research_text = (
        f"Firm: {firm_name}\n"
        f"News: {(data.get('news') or {}).get('summary') or 'none found'}\n"
        f"Funding history: {(data.get('funding') or {}).get('summary') or 'none found'}\n"
    )
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=900,
        system=COLD_FUND_BRIEF_PROMPT,
        messages=[{"role": "user", "content": research_text}],
    )
    return resp.content[0].text.strip()
