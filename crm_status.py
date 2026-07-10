"""
Capital-raise status report — the "where are we at" one-pager for Joe's manager
(or a fund GP he places for). Distinct from the daily `todo` digest (crm_todo,
tactical: what to do this week) and the `report` PDF (crm_dashboard, raw activity
metrics). This is the executive narrative: pipeline funnel, top prospects, investor
feedback themes, and progress — the summary you'd put in front of the person you
report to.

Signal gathering is deterministic SQL/scoring; the prose ("where we are",
"feedback", "next steps") is synthesized by Claude from those signals. With thin
data it stays honest — it reports what's on file and says so rather than inventing
a pipeline. Rendered to a Cedar Ridge letterhead PDF by crm_pdf.generate_status_pdf.
"""
from __future__ import annotations
import json
import os
import time as _time
from datetime import datetime, timezone

import psycopg2.extras
from anthropic import AsyncAnthropic

import store
import crm_dashboard
import crm_score

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

DAY = 86400

# Canonical pipeline order (matches crm_pdf). The "funnel" is the worked portion —
# New is cold top-of-funnel (a bulk contact import) and would dwarf every other bar,
# so it's surfaced as an overview metric, not plotted. Terminal stages sit outside
# the active funnel too.
STAGE_ORDER = [
    "New", "Contacted", "Engaged", "Intro made", "Materials sent",
    "Call scheduled", "Diligence", "Soft circled", "Committed", "Passed", "Dormant",
]
FUNNEL_STAGES = [
    "Contacted", "Engaged", "Intro made", "Materials sent",
    "Call scheduled", "Diligence", "Soft circled", "Committed",
]

STATUS_PROMPT = """You are the chief of staff for Joe Azzaro, a fund manager at Cedar Ridge Capital raising capital from LPs. Write the narrative for a one-page STATUS REPORT that Joe sends to the person he reports to (his manager / a fund GP he places for).

You're given live CRM signals. Write tight, executive prose — this is a summary of where the raise stands, not a task list. Be honest about how thin or full the data is: if there is little active pipeline on file, say so plainly rather than inflating it. NEVER invent investor names, dollar amounts, quotes, dates, or feedback that isn't in the signals.

DATES: Today's date is given below; relative times are `days_ago`. Derive calendar dates only from today minus days_ago or an explicit date field — never guess one.

Write EXACTLY these three sections, each header bolded on its own line, followed by 2-4 sentences (or a few "-" bullets). No other sections, no preamble, no sign-off:

<b>Where We Are</b>
The state of the raise: how many relationships are active vs. sourced, which stages hold the real pipeline, and the overall trajectory. One tight paragraph.

<b>Investor Feedback</b>
Themes from what LPs have actually said — objections raised, what's resonating, common concerns — drawn only from the feedback/objection signals. If there's little on file, say the feedback captured so far is limited and name what IS there.

<b>Next Steps</b>
- The 2-4 most important moves to advance the raise, tied to real firms/prospects in the signals.

FORMATTING: Bold with <b>...</b> ONLY — no Markdown. Use "-" for bullets. Keep the WHOLE thing under ~130 words — it must fit on one page alongside the charts and tables, so be ruthless and cut any sentence that isn't carrying weight."""


def _days_ago(ts, now):
    return max(0, (now - ts) // DAY) if ts else None


def _gather_feedback(now: int) -> dict:
    """Investor feedback = objections on file, at both the relationship and the
    per-opportunity level, plus recent negative/cautious interaction summaries."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Objection buckets tallied across everyone who has them (person + opp level).
            bucket_counts: dict[str, int] = {}
            cur.execute("SELECT objection_profile FROM crm_people WHERE objection_profile <> '{}'::jsonb")
            for r in cur.fetchall():
                for k in (r["objection_profile"] or {}):
                    bucket_counts[k] = bucket_counts.get(k, 0) + 1
            cur.execute("SELECT objection_profile FROM crm_opportunities WHERE objection_profile <> '{}'::jsonb")
            for r in cur.fetchall():
                for k in (r["objection_profile"] or {}):
                    bucket_counts[k] = bucket_counts.get(k, 0) + 1

            # A few concrete objection notes, with who raised them, for grounding.
            cur.execute("""
                SELECT p.name, f.name AS firm_name, p.objections
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.objections IS NOT NULL AND p.objections <> ''
                LIMIT 12
            """)
            person_objections = cur.fetchall()
            cur.execute("""
                SELECT o.product, o.objections, f.name AS firm_name, p.name
                FROM crm_opportunities o
                LEFT JOIN crm_people p ON p.id = o.person_id
                LEFT JOIN crm_firms f ON f.id = COALESCE(o.firm_id, p.firm_id)
                WHERE o.objections IS NOT NULL AND o.objections <> ''
                LIMIT 12
            """)
            opp_objections = cur.fetchall()

            # Recent interactions that read cautious/negative — the raw voice of feedback.
            cur.execute("""
                SELECT i.summary, i.sentiment, p.name, f.name AS firm_name, p.stage
                FROM crm_interactions i
                JOIN crm_people p ON p.id = i.person_id
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE i.ts > %s AND i.direction = 'inbound'
                ORDER BY i.ts DESC LIMIT 20
            """, (now - 90 * DAY,))
            recent_inbound = cur.fetchall()

    return {
        "objection_buckets": dict(sorted(bucket_counts.items(), key=lambda kv: kv[1], reverse=True)),
        "person_objections": person_objections,
        "opp_objections": opp_objections,
        "recent_inbound": recent_inbound,
    }


def gather(days: int = 30) -> dict:
    """Everything the status one-pager needs. Reuses crm_dashboard for base activity
    metrics and crm_score for the ranked top-prospects list."""
    now = int(_time.time())
    dash = crm_dashboard.gather(days=days)
    stage_counts = dash.get("stage_counts", {})

    # Top prospects: score every active person, keep the ranked head. Only prospects
    # with a real signal (past New, or a logged deal/importance) — a bare New import
    # with a zero score isn't a "top prospect".
    ranked = crm_score.rank_active_people()
    top_prospects = [
        r for r in ranked
        if (r.get("stage") not in (None, "New"))
        or r.get("deal_amount_usd")
        or (r.get("composite_score") or 0) > 0
    ][:8]

    # Opportunity stage distribution (deal-level funnel), plus the most recent opportunity
    # next-step per top prospect — the person-level next_step is often empty because the
    # live action lives on the opportunity record, so the report falls back to it.
    prospect_ids = [p["id"] for p in top_prospects if p.get("id")]
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT COALESCE(stage,'New') AS stage, COUNT(*) AS n FROM crm_opportunities GROUP BY 1")
            opp_stage_counts = {r["stage"]: r["n"] for r in cur.fetchall()}

            opp_next_step: dict[int, str] = {}
            if prospect_ids:
                cur.execute("""
                    SELECT DISTINCT ON (person_id) person_id, next_step
                    FROM crm_opportunities
                    WHERE person_id = ANY(%s) AND next_step IS NOT NULL AND next_step <> ''
                    ORDER BY person_id, updated_at DESC
                """, (prospect_ids,))
                opp_next_step = {r["person_id"]: r["next_step"] for r in cur.fetchall()}

    for p in top_prospects:
        p["next_step_display"] = p.get("next_step") or opp_next_step.get(p.get("id")) or ""

    return {
        "days": days,
        "generated_at": now,
        "total_contacts": dash.get("total_contacts", 0),
        "total_firms": dash.get("total_firms", 0),
        "total_interactions": dash.get("total_interactions", 0),
        "pipeline_count": dash.get("pipeline_count", 0),
        "pipeline_total_usd": dash.get("pipeline_total_usd", 0.0),
        "new_count": stage_counts.get("New", 0),
        "active_count": sum(v for s, v in stage_counts.items() if s in FUNNEL_STAGES),
        "stage_counts": stage_counts,
        "opp_stage_counts": opp_stage_counts,
        "funnel": [(s, stage_counts.get(s, 0)) for s in FUNNEL_STAGES if stage_counts.get(s, 0)],
        "transactions": dash.get("transactions", []),
        "meetings": dash.get("meetings", []),
        "top_prospects": top_prospects,
        "feedback": _gather_feedback(now),
    }


def _compact(data: dict) -> str:
    """Slim the gathered signals down to what the narrative model needs."""
    now = data["generated_at"]
    fb = data["feedback"]
    return json.dumps({
        "totals": {
            "contacts": data["total_contacts"], "firms": data["total_firms"],
            "sourced_not_worked_New": data["new_count"], "active_in_funnel": data["active_count"],
            "interactions_last_%dd" % data["days"]: data["total_interactions"],
            "active_opportunities": data["pipeline_count"],
        },
        "contact_stage_counts": data["stage_counts"],
        "opportunity_stage_counts": data["opp_stage_counts"],
        "top_prospects": [
            {"name": p.get("name") or p.get("email"), "firm": p.get("firm_name"),
             "stage": p.get("stage"), "score": p.get("composite_score"),
             "next_step": p.get("next_step_display") or p.get("next_step"),
             "days_since_touch": _days_ago(p.get("last_touch_ts"), now)}
            for p in data["top_prospects"]
        ],
        "active_opportunities": [
            {"product": t.get("product"), "firm": t.get("firm_name") or t.get("contact_or_firm"),
             "stage": t.get("stage"), "next_step": t.get("next_step")}
            for t in data["transactions"][:12]
        ],
        "feedback": {
            "objection_themes": fb["objection_buckets"],
            "objection_notes": [
                {"who": o.get("name"), "firm": o.get("firm_name"), "objection": o.get("objections")}
                for o in fb["person_objections"]
            ] + [
                {"product": o.get("product"), "firm": o.get("firm_name"), "objection": o.get("objections")}
                for o in fb["opp_objections"]
            ],
            "recent_inbound": [
                {"who": r.get("name"), "firm": r.get("firm_name"),
                 "sentiment": r.get("sentiment"), "summary": r.get("summary")}
                for r in fb["recent_inbound"][:12]
            ],
        },
    }, default=str)


async def narrative(data: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    import crm_store  # local import: directives block, keeps module import order simple
    resp = await claude.messages.create(
        model=MODEL, max_tokens=700,
        system=STATUS_PROMPT + crm_store.directives_prompt_block(),
        messages=[{"role": "user",
                   "content": f"Today's date: {today}\n\nCRM signals (relative times in days_ago):\n{_compact(data)}"}],
    )
    return resp.content[0].text.strip()


async def generate(days: int = 30) -> tuple[dict, str]:
    """Returns (data, narrative_text) for crm_pdf.generate_status_pdf."""
    data = gather(days=days)
    text = await narrative(data)
    return data, text
