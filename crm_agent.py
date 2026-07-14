"""
Cedar Ridge Inbox Agent — Phase 1: dedicated inbox ingestion + relationship memory.
Polls agent@cedarridge.capital, extracts structured relationship data with Claude,
and writes person/firm/interaction records. Everything the agent needs to tell the
user — alerts, digests, confirmations, replies — goes out as email (crm_mail.py),
not Telegram. The same inbox doubles as the command line: emailing the agent
"brief jane@x.com" or "confirm jane@x.com" works the same as the Telegram commands.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import traceback
from email.utils import parseaddr

import crm_ask
import crm_brief
import crm_coder
import crm_config
import crm_dashboard
import crm_directives
import crm_docs
import crm_draft
import crm_enrich
import crm_import
import crm_mail
import crm_mailbox
import crm_parser
import crm_pdf
import crm_radar
import crm_roadshow
import crm_score
import crm_status
import crm_store
import crm_todo
import crm_voice

logger = logging.getLogger(__name__)

try:
    # `or` handles the var being set-but-empty ('' -> default), which int(getenv(..., default))
    # does NOT — an empty env value crash-loops the container on boot.
    POLL_INTERVAL = int(os.getenv("CRM_POLL_INTERVAL_SECONDS") or 15 * 60)
except ValueError:
    POLL_INTERVAL = 15 * 60
HIGH_IMPORTANCE_THRESHOLD = 4

_PIPELINE_RE = re.compile(r"^pipeline\s*$", re.I)
_RADAR_RE = re.compile(r"^radar\s*$", re.I)
_CONFIRM_RE = re.compile(r"^confirm\s+(.+)$", re.I)
_REJECT_RE = re.compile(r"^reject\s+(.+)$", re.I)
_WHOIS_RE = re.compile(r"^whois\s+(.+)$", re.I)
_SCORE_RE = re.compile(r"^score\s+(.+)$", re.I)
_BRIEF_RE = re.compile(r"^brief\s+([^:]+?)(?::\s*(.+))?$", re.I)
_DRAFT_RE = re.compile(r"^draft\s+([^:]+):?\s*(.*)$", re.I)
_WHO_IN_RE = re.compile(r"^who(?:'s|s|\s+is|\s+are)\s+in\s+(.+)$", re.I)
_HIGH_PRIORITY_RE = re.compile(r"^(?:mark\s+|set\s+)?high.?priority\s+(.+)$", re.I)
_UNHIGH_PRIORITY_RE = re.compile(r"^(?:remove|unset|clear)\s+high.?priority\s+(.+)$", re.I)
_HELP_RE = re.compile(r"^(?:help|commands|\?)\s*$", re.I)
_MANUAL_RE = re.compile(r"^manual\s*$", re.I)
_TODO_RE = re.compile(r"^(?:daily\s+)?(?:todo|to-do|to do|dashboard|my day|day)\s*$", re.I)
# Free-form focus/priority questions ("what should I focus on this week?", "my priorities",
# "what's most important") route to the same rich dashboard rather than the thin Q&A path.
_FOCUS_RE = re.compile(
    r"(what\s+should\s+i\s+(?:focus|work|do|prioriti)|focus\s+on|priorit(?:y|ies|ise|ize)"
    r"|most\s+important|what'?s\s+important|what\s+to\s+do|(?:this|my)\s+week)",
    re.I,
)
# "how do you score …", "how are LPs scored", "scoring methodology", "explain the scoring" —
# a question ABOUT the scoring model, not a `score <name>` lookup. Needs both a scoring word
# and a how/explain/methodology cue, so it won't hijack "what's Jane's score".
_SCORING_HELP_RE = re.compile(
    r"(?=.*\bscor(?:e|es|ed|ing)\b)(?=.*\b(how|explain|methodolog|calculat|comput|derive)\b)",
    re.I | re.S,
)
_OPP_RE = re.compile(r"^opportunity\s+([^:]+):\s*(.+)$", re.I)
_UPDATE_RE = re.compile(r"^update\s+([^:]+):\s*(.+)$", re.I)
_DONE_RE = re.compile(r"^done\s+(.+)$", re.I)
_REPORT_RE = re.compile(r"^(?:crm\s+)?report(?:\s*:\s*(\d+)\s*d(?:ays)?)?\s*$", re.I)
# Owner-only executive status one-pager (crm_status): "status", "status report",
# "fund status", "manager report", optional ": 30 days" window like `report`.
_STATUS_RE = re.compile(
    r"^(?:status(?:\s+report)?|fund\s+status|(?:manager|raise)\s+(?:status\s+)?report)"
    r"(?:\s*:\s*(\d+)\s*d(?:ays)?)?\s*$", re.I,
)
# Roadshow: "roadshow LA" / "road show New York: Nebari". The `draft` form must be
# tried first so "draft roadshow LA" doesn't match the plain roadshow verb (or the
# generic `draft <contact>` command). Owner-only.
_DRAFT_ROADSHOW_RE = re.compile(r"^draft\s+road\s?show\s+([^:]+?)(?::\s*(.+))?\s*$", re.I)
_ROADSHOW_RE = re.compile(r"^road\s?show\s+([^:]+?)(?::\s*(.+))?\s*$", re.I)
# Narrow: a roadshow / trip note — keeps the generic `draft <contact>` command from hijacking
# "draft an email for a roadshow ..." (which has no contact) away from the roadshow handler.
_ROADSHOW_HINT_RE = re.compile(r"road\s?show|\btrip\b", re.I)
# Broad: any investor / prospect / target-list intent. A request for a LIST of investors must
# never be answered with the daily to-do dashboard (owner feedback), so this guards the fuzzy
# focus/priority catch-all in _handle_command — those notes fall through to the roadshow
# classifier or relationship Q&A instead of crm_todo.
_LIST_INTENT_RE = re.compile(
    r"road\s?show|target list|list of (?:investor|prospect|lp|name|contact)"
    r"|\b(?:investors?|prospects?|lps?)\b|who\s+(?:should|do)\s+i\s+(?:meet|see|target)|\btrip\b",
    re.I,
)
# Manual location tag: "set location Fairbridge: Los Angeles" (firm or contact). Owner-only.
_LOCATION_RE = re.compile(r"^(?:set\s+|tag\s+)?location\s+([^:]+):\s*(.+)$", re.I)

HELP_TEXT = """<b>Cedar Ridge Inbox Agent — commands</b>

You can email any of these, or just forward/BCC a relationship email and it gets parsed automatically.

<b>todo</b> (or <b>my day</b> / <b>dashboard</b>) — daily sales command center: today's priorities, this week's tasks, and deals at risk
<b>pipeline</b> — every contact grouped by stage, with deal size and next step
<b>radar</b> — run the follow-up digest on demand
<b>whois &lt;name or email&gt;</b> — full profile: phone, email, firm, stage, channel, notes
<b>score &lt;name or email&gt;</b> — LP score (0-100) with the full signal breakdown
<b>brief &lt;name or email&gt;</b> — one-page pre-call brief (Cedar Ridge letterhead PDF, plus plain text)
<b>brief &lt;name or email&gt;: &lt;product&gt;</b> — same, scoped to one specific opportunity's stage/notes/objections instead of the whole relationship
<b>report</b> (or <b>report: 7 days</b>) — CRM activity report PDF with charts: interaction volume, meetings/calls, pipeline stage distribution, prospective transactions. Defaults to trailing 30 days.
<b>status</b> (or <b>status report</b> / <b>fund status</b>) — owner-only executive one-pager for your manager/fund on Cedar Ridge letterhead: where the raise stands, pipeline funnel charts, top prospects, investor feedback, and next steps. Defaults to trailing 30 days.
<b>roadshow &lt;city&gt;</b> (or <b>roadshow &lt;city&gt;: &lt;product&gt;</b>) — owner-only trip planner: who to meet in a city, tiered into anchors (warm, meet in person), meeting candidates, and prospects just outside the city worth traveling for. E.g. "roadshow LA: Nebari". Then <b>draft roadshow &lt;city&gt;</b> generates copy-paste meeting-request emails for them.
<b>set location &lt;firm or contact&gt;: &lt;city&gt;</b> — tag where an investor is based (e.g. "set location Fairbridge: Los Angeles"), used by roadshow. Locations are also inferred automatically, but your tags win.
<b>draft &lt;name or email&gt;: &lt;instruction&gt;</b> — draft a reply for your review (never auto-sent)
<b>who is in &lt;stage&gt;</b> — e.g. "who is in diligence", "who's in engaged"
<b>high priority &lt;name or email&gt;</b> — always flag this contact in the daily digest
<b>remove high priority &lt;name or email&gt;</b> — undo that
<b>opportunity &lt;name or email&gt;: &lt;product&gt;, &lt;stage&gt;, &lt;amount&gt;, &lt;next step&gt;</b> — log a product opportunity for a contact (e.g. "opportunity Eric Derrington: Cedar Ridge Fund II, Diligence, $5m, send deck")
<b>done &lt;name or email&gt;</b> — mark the current next step as complete and clear it
<b>update &lt;name or email&gt;: &lt;freeform note&gt;</b> — update any contact fields from natural language (e.g. "update Eric Derrington: family office, met at iConnections, cares about risk-adjusted returns, warm lead")
<b>confirm &lt;name or email&gt;</b> / <b>reject &lt;name or email&gt;</b> — accept or dismiss a pending pipeline stage change
<b>merge &lt;name&gt; and &lt;name&gt;</b> (free-form, no fixed syntax needed) — combine two duplicate contacts into one; also works by just replying "these are different people" or "merge them" to a duplicate-flag email
<b>help</b> — this list
<b>manual</b> — what this agent actually does, in plain English

You can also just ask in plain English, e.g. "who's warm for Nebari?" or "what happened with Acorn?"

To bulk-add contacts, email this inbox an Excel (.xlsx) or CSV attachment with columns like Name, Email, Phone, Firm, Stage, Deal Amount.

To log product notes from a document, email a PDF (fact sheet, deck, memo) — mention which contact and product it's for in your note if it's not obvious from the file itself, and it'll write a summary into that contact's opportunity for that product.
"""

MANUAL_TEXT = """<b>Cedar Ridge Inbox Agent — the manual</b>

Think of this inbox as a chief of staff for your raise. You don't update it — you just BCC or forward the emails that matter, and it remembers everything for you.

<b>How it remembers</b>
Every email you send here gets read by Claude, turned into a structured record (who, what firm, what mandate, what stage, what they care about, what they objected to), and merged into one profile per contact. No re-typing the same relationship twice.

<b>What it watches for you</b>
A daily <b>radar</b> flags who's gone cold, who owes you a reply, and which conversations have an open loop — before they slip.

<b>What it can tell you, instantly</b>
Ask in plain English — "who's warm for Nebari?", "what happened with Acorn?" — or use <b>whois</b>, <b>score</b>, or <b>pipeline</b> for a structured pull.

<b>What it does before a call</b>
Email "<b>brief</b> Jane Smith" before you dial and get a one-pager: history, objections, what to bring up, what not to.

<b>What it drafts (never sends)</b>
"<b>draft</b> Jane: following up on the deck" gets you a reply in your voice, waiting for your review. It never sends on its own.

<b>What it scores</b>
Every contact gets an LP score from real signals — engagement, responsiveness, stated interest — so "who's actually warm" stops being a guess.

<b>What it bulk-imports</b>
Email it a spreadsheet (.xlsx, .csv, or .numbers) and it merges the whole list in, flagging duplicates instead of overwriting silently.

Full command list: email "help".
"""


def _format_enrichment(person: dict) -> str:
    raw = person.get("enrichment")
    if not raw:
        return "Enrichment: none yet"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return "Enrichment: none yet"

    parts = []
    linkedin = data.get("linkedin") or {}
    if linkedin:
        url = linkedin.get("url") or linkedin.get("linkedin_profile_url")
        parts.append(f"LinkedIn: {url}" if url else "LinkedIn: profile data found")
    news = (data.get("news") or {}).get("summary")
    if news:
        parts.append(f"Recent news: {news}")
    funding = (data.get("funding") or {}).get("summary")
    if funding:
        parts.append(f"Funding history: {funding}")
    return "\n".join(parts) if parts else "Enrichment: none yet"


_OBJ_LABELS = {
    "fee_concern": "Fee",
    "liquidity_concern": "Liquidity",
    "duration_concern": "Duration",
    "strategy_fit_concern": "Strategy fit",
    "manager_size_concern": "Manager size",
    "track_record_concern": "Track record",
    "operational_diligence_concern": "Operational diligence",
    "headline_risk_concern": "Headline risk",
    "timing_concern": "Timing / budget",
    "existing_exposure_concern": "Already has exposure",
}


def _fmt_objection_profile(raw) -> str:
    if not raw:
        return ""
    try:
        profile = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return ""
    lines = [f"  {_OBJ_LABELS.get(k, k)}: {v}" for k, v in profile.items() if v]
    return "Objection profile:\n" + "\n".join(lines) + "\n" if lines else ""


async def _flag_firm_if_similar(firm_id: int, is_new_firm: bool, firm_name: str, message_id: str | None):
    """Mirrors the person-level duplicate-name flag: when a brand-new firm is created
    and it looks like it might be the same company as an existing one (e.g. a legal-
    suffix variant that slipped past get_or_create_firm's own matching, or a genuine
    near-duplicate), don't silently merge or silently fragment — flag it and ask."""
    if not is_new_firm:
        return
    conflicts = crm_store.find_similar_firms(firm_name, firm_id)
    if not conflicts:
        return
    lines = "\n".join(f"  • {c['name']}" for c in conflicts)
    flag_message_id = await crm_mail.send_async(
        f"Duplicate firm flag: {firm_name}",
        f"<b>{firm_name}</b> looks like it might be the same company as an existing firm on file:\n\n"
        f"  • {firm_name} <i>(just created)</i>\n"
        f"{lines}\n\n"
        f"Reply to this email and tell me if these are the same company or should stay separate.",
    )
    if flag_message_id:
        for c in conflicts:
            crm_store.create_firm_flag(firm_id, c["id"], flag_message_id)


def _do_mark_done(contact_query: str) -> str:
    person = crm_store.find_person(contact_query)
    if not person:
        return f"No contact matching '{contact_query}'."
    prev = person.get("next_step") or "(none)"
    crm_store.clear_next_step(person["id"])
    return f"Cleared next step for {person.get('name') or person['email']}.\nWas: {prev}"


def _do_score(contact_query: str) -> str:
    result = crm_score.score_by_query(contact_query)
    if not result:
        return f"No contact matching '{contact_query}'."
    breakdown = "\n".join(
        f"  {k}: {v if v is not None else 'no data'}" for k, v in result["breakdown"].items()
    )
    return f"{result['name']} ({result.get('firm_name') or 'unknown firm'}): {result['composite_score']}/100\n{breakdown}"


async def _dispatch_action(action: dict) -> str | None:
    """Executes a classified free-form action (see crm_ask.try_action_command) — the
    natural-language equivalent of the done/priority/confirm/reject/score/draft/enrich regex
    commands in _handle_command. brief_request is handled separately by the caller since it
    needs msg-level context (PDF attachment, reply threading) this function doesn't have."""
    contact_query = (action.get("contact") or "").strip()
    if not contact_query:
        return None
    act = action["action"]
    if act == "mark_done":
        return _do_mark_done(contact_query)
    if act == "set_priority":
        return crm_ask.set_priority(contact_query, True)
    if act == "unset_priority":
        return crm_ask.set_priority(contact_query, False)
    if act == "confirm_stage":
        return crm_ask.confirm_stage(contact_query)
    if act == "reject_stage":
        return crm_ask.reject_stage(contact_query)
    if act == "score_query":
        return _do_score(contact_query)
    if act == "draft_request":
        return await crm_draft.generate(contact_query, action.get("instruction") or "Write a friendly check-in follow-up.")
    if act == "enrich_request":
        person = crm_store.find_person(contact_query)
        if person:
            await crm_enrich.enrich_person(person["id"], {
                "person_name": person.get("name"), "firm_name": person.get("firm_name"),
                "person_email": person.get("email"),
            })
            refreshed = crm_store.find_person(contact_query)
            name = refreshed.get("name") or refreshed["email"]
            return f"Researched {name}:\n{_format_enrichment(refreshed)}"
        # No contact on file — treat the query as a fund/firm name and research it
        # directly, so a fund doesn't need an existing contact before it can be
        # looked up (creates a bare-bones account so the research is saved for later).
        firm_id, _ = crm_store.get_or_create_firm(contact_query)
        if firm_id is None:
            return f"No contact or firm matching '{contact_query}'."
        data = await crm_enrich.enrich_firm(firm_id, contact_query)
        return f"Researched {contact_query}:\n{_format_enrichment({'enrichment': data})}"
    return None


async def _handle_command(note: str) -> str | None:
    """Recognizes the same command verbs as the Telegram bot. Returns None to fall through to free-form Q&A."""
    note = note.strip()

    if _HELP_RE.match(note):
        return HELP_TEXT
    if _MANUAL_RE.match(note):
        return MANUAL_TEXT
    if _TODO_RE.match(note):
        return await crm_todo.generate()
    if _PIPELINE_RE.match(note):
        return crm_ask.pipeline_summary()
    if _RADAR_RE.match(note):
        return crm_radar.build_digest() or "Nothing to flag right now."

    m = _CONFIRM_RE.match(note)
    if m:
        return crm_ask.confirm_stage(m.group(1))
    m = _REJECT_RE.match(note)
    if m:
        return crm_ask.reject_stage(m.group(1))
    m = _DONE_RE.match(note)
    if m:
        return _do_mark_done(m.group(1).strip())

    m = _WHOIS_RE.match(note)
    if m:
        person = crm_store.find_person(m.group(1).rstrip("?.,!"))
        if not person:
            return f"No contact matching '{m.group(1)}'."
        opps = crm_store.list_opportunities(person["id"])
        if opps:
            opp_text = "\nOpportunities:\n" + "\n".join(
                f"  • {o['product']}"
                + (f" ({o['category']})" if o.get('category') else "")
                + f" — {o['stage']}"
                + (f", ${o['deal_amount_usd']:,.0f}" if o.get('deal_amount_usd') else "")
                + (f" | next: {o['next_step']}" if o.get('next_step') else "")
                + (f"\n    Objections: {o['objections']}" if o.get('objections') else "")
                for o in opps
            )
            stage_line = ""
        else:
            opp_text = ""
            stage_line = f"Stage: {person.get('stage')}\n"
        def _field(label, val):
            return f"{label}: {val}\n" if val else ""

        return (
            f"{person.get('name') or person['email']}\n"
            f"Email: {person['email']}\n"
            + _field("Phone", person.get('phone'))
            + f"Firm: {person.get('firm_name') or 'unknown'}\n"
            + _field("Investor type", person.get('investor_type'))
            + _field("Role", person.get('role'))
            + stage_line
            + _field("Warmth", person.get('warmth'))
            + _field("Relationship", person.get('relationship_type'))
            + _field("How met", person.get('how_met'))
            + _field("Introduced by", person.get('introduced_by'))
            + _field("Connected via", person.get('contact_channel'))
            + _field("Communication style", person.get('communication_style'))
            + f"High priority: {'yes' if person.get('manual_priority') else 'no'}\n"
            + _field("Next step", person.get('next_step'))
            + _field("Mandate", person.get('mandate'))
            + _field("Cares about", person.get('cares_about'))
            + _field("Objections", person.get('objections'))
            + _fmt_objection_profile(person.get('objection_profile'))
            + _field("Passed on", person.get('passed_on'))
            + _field("Liked products", person.get('liked_products'))
            + _field("Revisit later", person.get('revisit_later'))
            + _field("Move forward conditions", person.get('move_forward_conditions'))
            + _field("Notes", person.get('notes'))
            + _field("Personal", person.get('personal_notes'))
            + _format_enrichment(person) + "\n"
            + opp_text
        )

    m = _WHO_IN_RE.match(note)
    if m:
        return crm_ask.stage_filter(m.group(1))

    m = _UNHIGH_PRIORITY_RE.match(note)
    if m:
        return crm_ask.set_priority(m.group(1), False)
    m = _HIGH_PRIORITY_RE.match(note)
    if m:
        return crm_ask.set_priority(m.group(1), True)

    m = _SCORE_RE.match(note)
    if m:
        return _do_score(m.group(1).rstrip("?.,!"))

    m = _UPDATE_RE.match(note)
    if m:
        contact_query, freeform = m.group(1).strip(), m.group(2).strip()
        person = crm_store.find_person(contact_query)
        if not person:
            return f"No contact matching '{contact_query}'."
        updates = await crm_ask.apply_update(person["id"], freeform)
        if not updates:
            return f"Couldn't extract any fields from that — try being more specific."
        summary = ", ".join(f"{k}: {v}" for k, v in updates.items())
        return f"Updated {person.get('name') or person['email']}:\n{summary}"

    m = _OPP_RE.match(note)
    if m:
        contact_query, opp_details = m.group(1).strip(), m.group(2).strip()
        # parse "Product, Stage, $Xm, next step" loosely
        parts = [p.strip() for p in opp_details.split(",")]
        product = parts[0] if parts else opp_details
        stage = parts[1] if len(parts) > 1 else None
        amount_raw = parts[2] if len(parts) > 2 else None
        next_step = parts[3] if len(parts) > 3 else None
        amount = None
        if amount_raw:
            try:
                clean = amount_raw.replace("$", "").replace(",", "").strip().lower()
                amount = float(clean[:-1]) * 1_000_000 if clean.endswith("m") else float(clean[:-1]) * 1_000 if clean.endswith("k") else float(clean)
            except ValueError:
                next_step = amount_raw  # wasn't a number, treat as next step
                amount = None
        person = crm_store.find_person(contact_query)
        if person:
            crm_store.upsert_opportunity(person["id"], product, stage, amount, next_step)
            target = person.get("name") or person["email"]
            note_line = ""
        else:
            # No contact/firm on file — never refuse. Create (or reuse) an account for the named
            # firm, anchor the opportunity there, and kick off best-effort public enrichment so a
            # placeholder fills in over time. (Per owner: lack of firm info must not block a deal.)
            firm_id, is_new = crm_store.get_or_create_firm(contact_query)
            if firm_id is None:
                return f"Couldn't create an account for '{contact_query}'."
            crm_store.upsert_opportunity(None, product, stage, amount, next_step, firm_id=firm_id)
            target = contact_query
            if is_new:
                asyncio.create_task(crm_enrich.enrich_firm(firm_id, contact_query))
                note_line = "\n(New account created with no contact on file — researching public info.)"
            else:
                note_line = "\n(Logged at the firm level — no specific contact on file.)"
        return (
            f"Opportunity logged for {target}:\n"
            f"Product: {product}\nStage: {stage or 'New'}"
            + (f"\nAmount: ${amount:,.0f}" if amount else "")
            + (f"\nNext step: {next_step}" if next_step else "")
            + note_line
        )

    # Skip the generic draft command for roadshow-draft notes ("draft an email for a roadshow
    # through LA and SF") — they have no contact and belong to the roadshow handler downstream.
    m = _DRAFT_RE.match(note)
    if m and not _ROADSHOW_HINT_RE.search(note):
        query, instruction = m.group(1).strip(), m.group(2).strip()
        return await crm_draft.generate(query, instruction or "Write a friendly check-in follow-up.")

    # Fuzzy catch-alls — checked LAST, after every structured command, so a command that
    # happens to contain a trigger word ("high priority jane@x.com" contains "priority"; an
    # opportunity/update note may contain "this week") isn't hijacked away from its real handler.
    if _SCORING_HELP_RE.search(note):
        return crm_score.explain_methodology()
    # ...but an investor-list / roadshow ask often contains focus/priority words ("investors we
    # should FOCUS ON", "top targets ranked by PRIORITY") — those want a LIST, never the to-do
    # dashboard, so let them fall through to the roadshow classifier / relationship Q&A.
    if _FOCUS_RE.search(note) and not _LIST_INTENT_RE.search(note):
        return await crm_todo.generate()

    return None


async def _send_brief(msg: dict, query: str, product: str | None = None):
    """Briefs go out as a Cedar Ridge letterhead PDF attachment, not just plain text —
    same template pdf_gen.py already uses for research memos. product, if given, scopes
    the brief to one specific opportunity instead of the contact's whole relationship."""
    try:
        brief_text = await crm_brief.generate(query, product=product)
        person = crm_store.find_person(query)
        if person:
            title = person.get("name") or person["email"]
            subtitle = f"{person.get('firm_name')} — {product}" if product and person.get("firm_name") else product or person.get("firm_name")
        else:
            people = crm_store.find_people_by_firm(query)
            title = people[0].get("firm_name") if people else query
            subtitle = None
        filename_suffix = f" - {product}" if product else ""
        pdf_bytes = crm_pdf.generate_brief_pdf(title, subtitle, brief_text)
        await crm_mail.send_async(
            f"Re: {msg.get('subject') or 'brief'}",
            f"Brief for {title}{filename_suffix} attached.\n\n{brief_text}",
            in_reply_to=msg.get("message_id"),
            attachment=(f"{title}{filename_suffix} Brief.pdf", pdf_bytes),
        )
    except Exception as e:
        logger.error(f"crm_agent: brief PDF generation failed: {e}")
        crm_store.log_event("error", {
            "module": "crm_agent._send_brief", "error": str(e), "traceback": traceback.format_exc(),
        })
        await crm_mail.send_async(
            f"Re: {msg.get('subject') or 'brief'}", f"Hit an error generating that brief: {e}",
            in_reply_to=msg.get("message_id"),
        )


async def _send_report(msg: dict, days: int = 30):
    """CRM activity report — interaction volume, meetings/calls, pipeline stage
    distribution, and prospective transactions, as a letterhead PDF with charts.
    Pure DB query + formatting (crm_dashboard), no LLM call needed."""
    try:
        data = crm_dashboard.gather(days=days)
        pdf_bytes = crm_pdf.generate_dashboard_pdf(data)
        summary = (
            f"Report attached — trailing {days} days.\n"
            f"{data['total_contacts']} contacts, {data['total_firms']} firms, "
            f"{data['total_interactions']} interactions this window, "
            f"{data['pipeline_count']} active opportunities "
            f"({_fmt_usd_short(data['pipeline_total_usd'])})."
        )
        await crm_mail.send_async(
            f"Re: {msg.get('subject') or 'report'}",
            summary,
            in_reply_to=msg.get("message_id"),
            attachment=("Cedar Ridge CRM Activity Report.pdf", pdf_bytes),
        )
    except Exception as e:
        logger.error(f"crm_agent: report PDF generation failed: {e}")
        crm_store.log_event("error", {
            "module": "crm_agent._send_report", "error": str(e), "traceback": traceback.format_exc(),
        })
        await crm_mail.send_async(
            f"Re: {msg.get('subject') or 'report'}", f"Hit an error generating that report: {e}",
            in_reply_to=msg.get("message_id"),
        )


async def _send_status_report(msg: dict, days: int = 30):
    """Capital-raise status one-pager (crm_status) — where the raise stands, pipeline
    funnel, top prospects, investor feedback, next steps — as a Cedar Ridge letterhead
    PDF for Joe's manager/fund. Owner-only (gated by the caller)."""
    try:
        data, narrative_text = await crm_status.generate(days=days)
        pdf_bytes = crm_pdf.generate_status_pdf(data, narrative_text)
        summary = (
            f"Status report attached — trailing {days} days.\n"
            f"{data['active_count']} active relationships, "
            f"{data['pipeline_count']} active opportunities, "
            f"{data['new_count']} sourced (not yet worked).\n\n"
            f"{narrative_text}"
        )
        await crm_mail.send_async(
            f"Re: {msg.get('subject') or 'status report'}",
            summary,
            in_reply_to=msg.get("message_id"),
            attachment=("Cedar Ridge Capital Raise - Status Report.pdf", pdf_bytes),
        )
    except Exception as e:
        logger.error(f"crm_agent: status report generation failed: {e}")
        crm_store.log_event("error", {
            "module": "crm_agent._send_status_report", "error": str(e), "traceback": traceback.format_exc(),
        })
        await crm_mail.send_async(
            f"Re: {msg.get('subject') or 'status report'}", f"Hit an error generating that status report: {e}",
            in_reply_to=msg.get("message_id"),
        )


def _do_set_location(query: str, location: str) -> str:
    """Manual location tag. Prefers the firm (a city usually applies to the whole firm),
    falling back to a specific contact, and creating a bare firm if neither exists yet.
    Manual tags are authoritative — cached LLM inference never overwrites them."""
    query, location = query.strip().rstrip("?.,!"), location.strip()
    firm = crm_store.find_firm_by_name(query)
    if firm:
        crm_store.set_firm_location(firm["id"], location, source="manual")
        return f"Tagged <b>{firm['name']}</b> as based in <b>{location}</b>."
    person = crm_store.find_person(query)
    if person:
        crm_store.set_person_location(person["id"], location, source="manual")
        name = person.get("name") or person["email"]
        return f"Tagged <b>{name}</b> as based in <b>{location}</b>."
    firm_id, _ = crm_store.get_or_create_firm(query)
    if firm_id is None:
        return f"Couldn't find or create a firm/contact for '{query}'."
    crm_store.set_firm_location(firm_id, location, source="manual")
    return f"Created firm <b>{query}</b> and tagged it as based in <b>{location}</b>."


def _fmt_usd_short(amount: float | None) -> str:
    if not amount:
        return "$0"
    if amount >= 1_000_000:
        return f"${amount/1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount/1_000:.0f}K"
    return f"${amount:,.0f}"


async def _reply_text(msg: dict, sender: str, reply: str):
    """Send a plain text reply back to the sender, threaded on the original message."""
    await crm_mail.send_async(
        f"Re: {msg.get('subject') or 'your note'}", reply,
        in_reply_to=msg.get("message_id"), to=sender,
    )


async def _reply_to_note(msg: dict, note: str, _decomposed: bool = False):
    # Multi-task: an email may ask for several distinct things ("today's to-do AND a status
    # report", "a roadshow target list AND draft the outreach emails"). Split it into standalone
    # tasks and run each through routing (one reply per task). Single requests — the common case —
    # and code-change commands come back as one item and fall through unchanged. _decomposed
    # guards against infinite recursion on the per-task calls.
    if not _decomposed:
        tasks = await crm_ask.decompose_tasks(note)
        if len(tasks) > 1:
            for t in tasks:
                await _reply_to_note(msg, t, _decomposed=True)
            return

    m = _BRIEF_RE.match(note.strip())
    if m:
        contact_query = m.group(1).strip().rstrip("?.,!")
        product = m.group(2).strip().rstrip("?.,!") if m.group(2) else None
        await _send_brief(msg, contact_query, product)
        return

    m = _REPORT_RE.match(note.strip())
    if m:
        days = int(m.group(1)) if m.group(1) else 30
        await _send_report(msg, days)
        return

    sender = parseaddr(msg.get("from") or "")[1]
    from_owner = crm_mail.is_owner(sender)

    m = _STATUS_RE.match(note.strip())
    if m:
        # Owner-only: the status one-pager is an internal report for Joe's manager/fund,
        # not something a forwarded contact or LP should be able to pull.
        if not from_owner:
            await crm_mail.send_async(
                f"Re: {msg.get('subject') or 'status'}",
                "The status report is available to the account owner only.",
                in_reply_to=msg.get("message_id"), to=sender,
            )
            return
        days = int(m.group(1)) if m.group(1) else 30
        await _send_status_report(msg, days)
        return

    # Roadshow + manual location tag are owner-only internal planning tools. The `draft`
    # form is matched before the plain roadshow verb (and before the generic draft command).
    for _rs_re, _rs_fn in ((_DRAFT_ROADSHOW_RE, crm_roadshow.draft_emails),
                           (_ROADSHOW_RE, crm_roadshow.roadshow)):
        m = _rs_re.match(note.strip())
        if m:
            if not from_owner:
                await _reply_text(msg, sender, "Roadshow planning is available to the account owner only.")
                return
            city = m.group(1).strip().rstrip("?.,!")
            product = m.group(2).strip().rstrip("?.,!") if m.group(2) else None
            await _reply_text(msg, sender, await _rs_fn(city, product))
            return

    m = _LOCATION_RE.match(note.strip())
    if m:
        if not from_owner:
            await _reply_text(msg, sender, "Tagging locations is available to the account owner only.")
            return
        await _reply_text(msg, sender, _do_set_location(m.group(1), m.group(2)))
        return

    try:
        # Level 3: a code-change request (carries the shared secret) is handled exclusively.
        reply = await crm_coder.try_code_command(note, from_owner, msg.get("body") or note)
        if reply is None:
            reply = await _handle_command(note)
        if reply is None:
            reply = await crm_ask.try_merge_command(note)
        if reply is None:
            reply = await crm_ask.try_bulk_opportunity_update(note)
        if reply is None:
            reply = await crm_ask.try_objection_command(note)
        if reply is None:
            action = await crm_ask.try_action_command(note)
            if action:
                if action["action"] == "brief_request":
                    await _send_brief(msg, action["contact"], action.get("product"))
                    return
                # Freeform routes to the same owner-only planning tools as the explicit commands.
                if action["action"] == "status_report":
                    if not from_owner:
                        await _reply_text(msg, sender, "The status report is available to the account owner only.")
                        return
                    await _send_status_report(msg)
                    return
                if action["action"] == "roadshow":
                    if not from_owner:
                        await _reply_text(msg, sender, "Roadshow planning is available to the account owner only.")
                        return
                    # wants_drafts -> the outreach emails; otherwise the tiered target list.
                    _rs_fn = crm_roadshow.draft_emails if action.get("wants_drafts") else crm_roadshow.roadshow
                    await _reply_text(msg, sender, await _rs_fn(
                        (action.get("city") or "").strip(), (action.get("product") or "").strip() or None))
                    return
                if action["action"] == "set_location":
                    if not from_owner:
                        await _reply_text(msg, sender, "Tagging locations is available to the account owner only.")
                        return
                    reply = _do_set_location(action.get("contact") or "", action.get("location") or "")
                else:
                    reply = await _dispatch_action(action)
        if reply is None:
            reply = await crm_config.try_config_command(note, from_owner)
        if reply is None:
            reply = await crm_directives.try_directive_command(note, from_owner)
        if reply is None:
            reply = await crm_ask.try_natural_update(note)
        if reply is None:
            reply = await crm_ask.answer(note)
    except Exception as e:
        logger.error(f"crm_agent: failed to answer direct note: {e}")
        crm_store.log_event("error", {
            "module": "crm_agent._reply_to_note",
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        reply = "Got your note but hit an error processing it. Logged for now."

    subject = f"Re: {msg.get('subject') or 'your note'}"
    body = f"<b>You wrote:</b> {note}\n\n{reply}"
    await crm_mail.send_async(subject, body, in_reply_to=msg.get("message_id"), to=sender)


async def _handle_pdf_attachments(pdf_atts: list[dict], email_note: str) -> list[str]:
    """Product/deal PDFs (fact sheets, decks, memos) — each is about one specific
    contact's one opportunity, never a standalone product record. Only writes when
    both the contact and product are confidently determined; otherwise asks."""
    summaries = []
    for att in pdf_atts:
        try:
            result = await crm_docs.extract_product_note(att["content"], att["filename"], email_note)
            if result.get("error"):
                summaries.append(f"{att['filename']}: couldn't read this PDF ({result['error']})")
                continue
            contact_query = (result.get("contact") or "").strip()
            product = (result.get("product") or "").strip()
            if not contact_query or not product:
                summaries.append(
                    f"{att['filename']}: couldn't tell which contact or product this is for — "
                    f"reply with e.g. 'this is for Jane Smith, Fund II'."
                )
                continue
            person = crm_store.find_person(contact_query)
            if not person:
                summaries.append(f"{att['filename']}: couldn't find a contact matching '{contact_query}'.")
                continue
            category = result.get("category")
            crm_store.upsert_opportunity(person["id"], product, notes=result.get("notes"), category=category)
            name = person.get("name") or person["email"]
            cat_suffix = f" ({category})" if category else ""
            summaries.append(f"{att['filename']}: logged notes on {name}'s '{product}'{cat_suffix} opportunity")
        except Exception as e:
            logger.error(f"crm_agent: PDF processing failed for {att['filename']}: {e}")
            summaries.append(f"{att['filename']}: processing failed — {e}")
    return summaries


async def _handle_attachments(msg: dict) -> bool:
    """If this email has a recognized contact-list or product-document attachment,
    process it and reply with a summary instead of running it through relationship/note
    extraction. Returns True if an attachment was handled (caller should skip the rest)."""
    attachments = msg.get("attachments") or []
    spreadsheet_atts = [a for a in attachments if a["filename"].lower().endswith(crm_mailbox.SPREADSHEET_EXTENSIONS)]
    pdf_atts = [a for a in attachments if a["filename"].lower().endswith(crm_mailbox.DOCUMENT_EXTENSIONS)]
    if not spreadsheet_atts and not pdf_atts:
        return False

    def _schedule_enrichment(person_id, extracted):
        asyncio.create_task(crm_enrich.enrich_person(person_id, extracted))

    body_parts = []
    all_duplicates = []
    all_conflicts = []

    if spreadsheet_atts:
        summaries = []
        for att in spreadsheet_atts:
            try:
                result = crm_import.import_contacts(att["content"], att["filename"], on_new_person=_schedule_enrichment)
                summaries.append(
                    f"{att['filename']}: {result['added']} added, {result['updated']} updated, "
                    f"{result['skipped']} skipped (of {result['total']} rows)"
                )
                all_duplicates.extend(result.get("duplicates") or [])
                all_conflicts.extend(result.get("name_conflicts") or [])
            except Exception as e:
                logger.error(f"crm_agent: import failed for {att['filename']}: {e}")
                summaries.append(f"{att['filename']}: import failed — {e}")
        body_parts.append("<b>Contact import results:</b>\n" + "\n".join(summaries))

    if pdf_atts:
        pdf_summaries = await _handle_pdf_attachments(pdf_atts, msg.get("body", ""))
        body_parts.append("<b>Document results:</b>\n" + "\n".join(pdf_summaries))

    body = "\n\n".join(body_parts)

    # Duplicates/conflicts are folded into this one email instead of one send per hit —
    # a big import can trip these dozens of times and used to flood the inbox.
    MAX_LISTED = 30
    if all_duplicates:
        lines = "\n".join(
            f"  • {d['name']} — {d.get('firm_name') or 'unknown firm'} (existing: {d['existing_email']})"
            for d in all_duplicates[:MAX_LISTED]
        )
        if len(all_duplicates) > MAX_LISTED:
            lines += f"\n  ...and {len(all_duplicates) - MAX_LISTED} more"
        body += f"\n\n<b>{len(all_duplicates)} duplicate(s) skipped (already in your CRM):</b>\n{lines}"

    if all_conflicts:
        lines = "\n".join(
            f"  • {c['name']} — {c.get('firm_name') or 'unknown firm'} ({c.get('email') or 'no email'}) <i>(just imported)</i>, "
            "also exists as: " + ", ".join(
                f"{x['name']} — {x.get('firm_name') or 'unknown firm'} ({x['email']})" for x in c["conflicts"]
            )
            for c in all_conflicts[:MAX_LISTED]
        )
        if len(all_conflicts) > MAX_LISTED:
            lines += f"\n  ...and {len(all_conflicts) - MAX_LISTED} more"
        body += (
            f"\n\n<b>{len(all_conflicts)} possible same-person-different-firm flag(s):</b>\n{lines}\n"
            "Reply to this email and tell me if these are different people or should be merged."
        )

    sent_message_id = await crm_mail.send_async(
        f"Re: {msg.get('subject') or 'contact import'}", body, in_reply_to=msg.get("message_id")
    )
    if sent_message_id:
        for c in all_conflicts:
            for other in c["conflicts"]:
                crm_store.create_name_flag(c["person_id"], other["id"], sent_message_id)
    return True


async def process_once():
    messages = crm_mailbox.fetch_new_messages()
    crm_store.log_event("poll_complete", {"message_count": len(messages)})
    for msg in messages:
        # claim FIRST: the rolling-window fetch re-returns already-processed mail every
        # poll, so skip silently on a dup (don't log it) and only announce genuinely-new mail.
        if not crm_store.claim_message(msg["message_id"]):
            continue

        crm_store.log_event("email_received", {
            "from": msg.get("from"),
            "subject": msg.get("subject"),
            "message_id": msg.get("message_id"),
        })

        audio_atts = [a for a in (msg.get("attachments") or []) if a["filename"].lower().endswith(crm_mailbox.AUDIO_EXTENSIONS)]
        if audio_atts:
            transcript = await crm_voice.transcribe_attachments(audio_atts)
            if transcript:
                body = (msg.get("body") or "").strip()
                msg["body"] = f"{body}\n\n{transcript}" if body else transcript
                crm_store.log_event("voice_note_transcribed", {
                    "from": msg.get("from"), "subject": msg.get("subject"), "chars": len(transcript),
                })
            audio_ids = {id(a) for a in audio_atts}
            msg["attachments"] = [a for a in msg["attachments"] if id(a) not in audio_ids]

        if await _handle_attachments(msg):
            continue

        pending_flags = crm_store.get_pending_flags_by_message_id(msg.get("in_reply_to"))
        if not pending_flags and not msg.get("in_reply_to"):
            # No thread match — only worth checking untargeted replies against every
            # pending flag if there actually are any outstanding (usually there aren't).
            pending_flags = crm_store.get_all_pending_flags()
        if pending_flags:
            resolution = await crm_ask.try_resolve_name_flags(msg.get("body", ""), pending_flags)
            if resolution:
                crm_store.log_event("name_flag_resolved", {"from": msg.get("from"), "resolution": resolution[:500]})
                await crm_mail.send_async(
                    f"Re: {msg.get('subject') or 'your note'}", resolution, in_reply_to=msg.get("message_id")
                )
                continue

        pending_firm_flags = crm_store.get_pending_firm_flags_by_message_id(msg.get("in_reply_to"))
        if not pending_firm_flags and not msg.get("in_reply_to"):
            pending_firm_flags = crm_store.get_all_pending_firm_flags()
        if pending_firm_flags:
            resolution = await crm_ask.try_resolve_firm_flags(msg.get("body", ""), pending_firm_flags)
            if resolution:
                crm_store.log_event("firm_flag_resolved", {"from": msg.get("from"), "resolution": resolution[:500]})
                await crm_mail.send_async(
                    f"Re: {msg.get('subject') or 'your note'}", resolution, in_reply_to=msg.get("message_id")
                )
                continue

        extracted = await crm_parser.extract(msg)
        if extracted.get("skip"):
            crm_store.log_event("email_skipped", {"from": msg.get("from"), "subject": msg.get("subject")})
            continue

        if extracted.get("intent") == "direct_note":
            note = extracted.get("note_content") or msg.get("body", "")
            logger.info(f"crm_agent: direct note received: {note[:200]}")
            crm_store.log_event("direct_note", {"from": msg.get("from"), "note": note[:300]})
            await _reply_to_note(msg, note)
            continue

        if extracted.get("intent") == "call_note":
            extracted["direction"] = "outbound"
            logger.info(f"crm_agent: call note — {extracted.get('person_name')} ({extracted.get('firm_name')})")

        firm_id, is_new_firm = crm_store.get_or_create_firm(extracted.get("firm_name"))
        await _flag_firm_if_similar(firm_id, is_new_firm, extracted.get("firm_name"), msg.get("message_id"))

        # A contact can be discussed for several distinct products/deals over time — each
        # gets its own crm_opportunities row (keyed on person + mandate), separate from the
        # single contact record, so a second deal for the same person doesn't get silently
        # dropped (upsert_person only ever fills the contact's own mandate field once). When
        # this email is clearly about one specific product, its objections AND next_step
        # belong to that product too — not the contact's own general fields, which would
        # otherwise blend one deal's action item into a contact discussing several.
        contact_extracted = extracted
        if extracted.get("mandate"):
            contact_extracted = {**extracted, "objections": None, "objection_profile": None, "next_step": None}

        person_id, is_new_person = crm_store.upsert_person(contact_extracted, firm_id, msg["ts"])

        if extracted.get("mandate"):
            crm_store.upsert_opportunity(
                person_id, extracted["mandate"],
                stage=extracted.get("stage"),
                deal_amount_usd=extracted.get("deal_amount_usd"),
                next_step=extracted.get("next_step"),
                notes=extracted.get("notes"),
                objections=extracted.get("objections"),
                objection_profile=extracted.get("objection_profile"),
            )

        # A joint email to several external people (e.g. two recipients at the same firm
        # discussing one mandate) names one counterparty in the top-level fields — anyone
        # else gets their own contact record here, sharing the same firm/mandate/stage
        # context, so they're not silently dropped from the CRM.
        claimed_emails = {(extracted.get("person_email") or "").strip().lower()} - {""}
        for extra in (extracted.get("additional_people") or []):
            extra_name = (extra.get("name") or "").strip()
            extra_email = (extra.get("email") or "").strip()
            if not extra_name and not extra_email:
                continue
            # A shared/generic mailbox (e.g. a firm's investor-relations inbox) can't be
            # reused as this person's email — upsert_person keys on email first, so reusing
            # an already-claimed address would match the other person's row and overwrite
            # their name instead of creating a distinct contact. Fall back to name+firm
            # matching in that case.
            if extra_email and extra_email.lower() in claimed_emails:
                extra_email = ""
            if extra_email:
                claimed_emails.add(extra_email.lower())
            extra_firm_name = extra.get("firm_name") or extracted.get("firm_name")
            if extra_firm_name == extracted.get("firm_name"):
                extra_firm_id = firm_id
            else:
                extra_firm_id, extra_is_new_firm = crm_store.get_or_create_firm(extra_firm_name)
                await _flag_firm_if_similar(extra_firm_id, extra_is_new_firm, extra_firm_name, msg.get("message_id"))
            extra_extracted = {
                **extracted,
                "person_name": extra_name or None,
                "person_email": extra_email or None,
                "role": extra.get("role"),
                "firm_name": extra_firm_name,
                "objections": None, "objection_profile": None, "next_step": None,
            }
            extra_person_id, extra_is_new = crm_store.upsert_person(extra_extracted, extra_firm_id, msg["ts"])
            if extracted.get("mandate"):
                crm_store.upsert_opportunity(
                    extra_person_id, extracted["mandate"],
                    stage=extracted.get("stage"), deal_amount_usd=extracted.get("deal_amount_usd"),
                    next_step=extracted.get("next_step"), notes=extracted.get("notes"),
                )
            crm_store.log_event("additional_contact_recorded", {
                "person": extra_name or extra_email, "firm": extra_firm_name,
                "primary_contact": extracted.get("person_name"), "is_new_person": extra_is_new,
            })

        is_new_interaction = crm_store.insert_interaction(
            person_id, msg["message_id"], msg["subject"], msg["direction"],
            msg["ts"], extracted, msg.get("body", ""),
        )

        if not is_new_interaction:
            continue

        logger.info(
            f"crm_agent: recorded interaction with {extracted.get('person_name') or extracted.get('person_email')} "
            f"(importance={extracted.get('importance')})"
        )
        crm_store.log_event("interaction_recorded", {
            "person": extracted.get("person_name") or extracted.get("person_email"),
            "firm": extracted.get("firm_name"),
            "importance": extracted.get("importance"),
            "is_new_person": is_new_person,
            "stage": extracted.get("stage"),
        })

        special_reply_sent = False

        person_name = extracted.get("person_name")
        if person_name:
            conflicts = crm_store.find_name_conflicts(person_name, person_id)
            if conflicts:
                lines = "\n".join(
                    f"  • {c['name']} — {c.get('firm_name') or 'unknown firm'} ({c['email']})"
                    for c in conflicts
                )
                flag_message_id = await crm_mail.send_async(
                    f"Duplicate name flag: {person_name}",
                    f"<b>{person_name}</b> appears in your CRM under multiple firms:\n\n"
                    f"  • {person_name} — {extracted.get('firm_name') or 'unknown firm'} ({extracted.get('person_email') or 'no email'}) <i>(just seen)</i>\n"
                    f"{lines}\n\n"
                    f"Reply to this email and tell me if these are different people or should be merged.",
                )
                if flag_message_id:
                    for c in conflicts:
                        crm_store.create_name_flag(person_id, c["id"], flag_message_id)
                    special_reply_sent = True

        if (extracted.get("importance") or 0) >= HIGH_IMPORTANCE_THRESHOLD:
            name = extracted.get("person_name") or extracted.get("person_email") or "Unknown"
            firm = extracted.get("firm_name") or ""
            label = f"{name} ({firm})" if firm else name
            body = (
                f"<b>High-priority contact: {label}</b>\n"
                f"{extracted.get('summary', '')}\n"
                f"Next step: {extracted.get('next_step') or 'none noted'}"
            )
            await crm_mail.send_async(f"High-priority contact: {label}", body)
            special_reply_sent = True

        person = crm_store.find_person(extracted.get("person_email") or "")
        if person and person.get("pending_stage"):
            await crm_mail.send_async(
                f"Stage change pending: {person.get('name') or person['email']}",
                f"<b>{person.get('name') or person['email']}</b>: {person['stage']} → {person['pending_stage']}\n"
                f"Reason: {person.get('pending_stage_reason') or 'n/a'}\n\n"
                f"Reply to this email with \"confirm {person['email']}\" or \"reject {person['email']}\".",
            )
            special_reply_sent = True

        if not special_reply_sent:
            # Every logged email gets some acknowledgment — nothing should process
            # silently with no visible confirmation that it was actually captured.
            name = extracted.get("person_name") or extracted.get("person_email") or extracted.get("firm_name") or "Unknown contact"
            firm = extracted.get("firm_name") or ""
            label = f"{name} ({firm})" if firm and firm not in name else name
            await crm_mail.send_async(
                f"Logged: {label}",
                f"<b>Logged: {label}</b>\n"
                f"Stage: {extracted.get('stage') or 'unchanged'}\n"
                f"{extracted.get('summary', '')}\n"
                + (f"Next step: {extracted['next_step']}\n" if extracted.get("next_step") else ""),
                in_reply_to=msg.get("message_id"),
            )

        if is_new_person:
            asyncio.create_task(crm_enrich.enrich_person(person_id, extracted))


async def run_loop():
    crm_store.init_crm_db()
    logger.info(f"crm_agent: starting poll loop every {POLL_INTERVAL}s")
    while True:
        crm_store.log_event("poll_start")
        try:
            await process_once()
        except Exception as e:
            logger.error(f"crm_agent: loop error: {e}")
            crm_store.log_event("error", {
                "module": "crm_agent.run_loop",
                "error": str(e),
                "traceback": traceback.format_exc(),
            })
        await asyncio.sleep(POLL_INTERVAL)
