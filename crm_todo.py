"""
Daily sales dashboard — email "todo", "my day", or "dashboard" to get a
prioritised view of the day and week: follow-ups, replies owed, cold
relationships, high-value opportunities needing attention, and slipping deals.

Signal gathering is deterministic SQL; narrative synthesis is Claude.
"""
from __future__ import annotations
import json
import os
import time as _time

import psycopg2.extras
from anthropic import AsyncAnthropic

import store
import crm_store

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

DAY = 86400
COLD_DAYS = 21
STALE_OPP_DAYS = 30
REPLY_OVERDUE_DAYS = 3
FOLLOW_UP_OVERDUE_DAYS = 7

DASHBOARD_PROMPT = """You are the chief of staff for Joe Azzaro, a fund manager at Cedar Ridge Capital raising capital from LPs.

You're given CRM signals pulled from his database. Write his weekly focus email — concise and executive. This goes to a busy fund manager: surface only what matters, no padding.

WHAT TO INCLUDE — be selective:
- Only surface investors/firms that have REAL activity: a logged opportunity, a genuine priority signal (importance, a deal amount, a live next step), or a recent interaction.
- IGNORE bare/unqualified contacts — a name with no mandate, no amount, no opportunity, and no logged interaction is almost always a stray import. NEVER present those as priorities or "to review". If in doubt, leave it out.

DATES: Today's date is given below; relative times are `days_ago`. NEVER invent or guess a calendar date — derive it from today minus days_ago or an explicit date field, else say "no date on file".

STRUCTURE (bold every header; group primarily BY INVESTOR):

For each investor/firm that genuinely matters, one block:
<b>[Investor / Firm name]</b>
Immediate priorities: the most time-sensitive item(s) — omit the line if none
Medium priority: secondary items — omit if none
Key action items: the specific next action(s) for this investor

Order investors by importance and deal value. Keep each block to a few tight lines. If an investor has several products/opportunities in play, note them within its block.

Then, once at the end:

<b>Manager Action Items</b>
A short, prioritised checklist of what Joe personally must do this week across all investors — calls to book, materials owed, follow-ups due. Real names, firms, amounts. Bullet each.

<b>Summary</b>
Two or three sentences: the state of the pipeline this week and where Joe should concentrate.

FORMATTING: HTML email. Bold with <b>...</b> ONLY — no Markdown (no "##", "**", or "---"). Use "-" for bullets, blank lines between blocks. Be concise.
"""


def _days_ago(ts: int | None, now: int) -> int | None:
    if not ts:
        return None
    return max(0, (now - ts) // DAY)


def _name(row) -> str:
    return row.get("name") or row.get("email") or "unknown"


def _firm(row) -> str:
    return f" ({row['firm_name']})" if row.get("firm_name") else ""


def _amt(row) -> str:
    amt = row.get("deal_amount_usd")
    if not amt:
        return ""
    if amt >= 1_000_000:
        return f" — ${amt/1_000_000:.1f}M"
    if amt >= 1_000:
        return f" — ${amt/1_000:.0f}K"
    return f" — ${amt:.0f}"


def _gather(now: int) -> dict:
    cold_days = crm_store.get_config_int("cold_days", COLD_DAYS)
    stale_opp_days = crm_store.get_config_int("stale_opp_days", STALE_OPP_DAYS)
    reply_overdue_days = crm_store.get_config_int("reply_overdue_days", REPLY_OVERDUE_DAYS)
    follow_up_overdue_days = crm_store.get_config_int("follow_up_overdue_days", FOLLOW_UP_OVERDUE_DAYS)
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # 1. Overdue follow-ups: next_step set, not touched in 7+ days
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage,
                       p.next_step, p.last_touch_ts, p.importance, p.deal_amount_usd
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.next_step IS NOT NULL AND p.next_step != ''
                  AND p.stage NOT IN ('Committed','Passed','Dormant')
                  AND (p.last_touch_ts IS NULL OR p.last_touch_ts < %s)
                ORDER BY p.importance DESC NULLS LAST, p.deal_amount_usd DESC NULLS LAST,
                         p.last_touch_ts ASC NULLS FIRST
                LIMIT 15
            """, (now - follow_up_overdue_days * DAY,))
            overdue = list(cur.fetchall())

            # 2. Unanswered inbound: last email from them, no reply in 3+ days
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage,
                       i.subject, i.summary, i.ts
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                JOIN crm_interactions i ON i.id = (
                    SELECT id FROM crm_interactions
                    WHERE person_id = p.id ORDER BY ts DESC LIMIT 1
                )
                WHERE i.direction = 'inbound'
                  AND i.ts < %s AND i.ts > %s
                  AND p.stage NOT IN ('Committed','Passed','Dormant')
                ORDER BY i.ts ASC
                LIMIT 10
            """, (now - reply_overdue_days * DAY, now - 30 * DAY))
            unanswered = list(cur.fetchall())

            # 3. Promised items: next_step says to send something
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage,
                       p.next_step, p.last_touch_ts, p.deal_amount_usd
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage NOT IN ('Committed','Passed','Dormant')
                  AND (p.next_step ILIKE '%send%' OR p.next_step ILIKE '%share%'
                    OR p.next_step ILIKE '%deck%' OR p.next_step ILIKE '%materials%'
                    OR p.next_step ILIKE '%data room%' OR p.next_step ILIKE '%provide%'
                    OR p.next_step ILIKE '%intro%' OR p.next_step ILIKE '%connect%')
                ORDER BY p.importance DESC NULLS LAST, p.deal_amount_usd DESC NULLS LAST
                LIMIT 10
            """)
            promised = list(cur.fetchall())

            # 4. Going cold: active, no touch in 21+ days
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage,
                       p.last_touch_ts, p.warmth, p.importance, p.deal_amount_usd
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage NOT IN ('Committed','Passed','Dormant')
                  AND (p.last_touch_ts IS NULL OR p.last_touch_ts < %s)
                ORDER BY p.importance DESC NULLS LAST, p.deal_amount_usd DESC NULLS LAST,
                         p.last_touch_ts ASC NULLS FIRST
                LIMIT 15
            """, (now - cold_days * DAY,))
            cold = list(cur.fetchall())

            # 5. High-value, no next step
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage,
                       p.deal_amount_usd, p.importance, p.last_touch_ts
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage NOT IN ('Committed','Passed','Dormant')
                  AND (p.importance >= 4 OR p.deal_amount_usd >= 500000)
                  AND (p.next_step IS NULL OR p.next_step = '')
                ORDER BY p.deal_amount_usd DESC NULLS LAST, p.importance DESC NULLS LAST
                LIMIT 10
            """)
            high_value_no_action = list(cur.fetchall())

            # 6. Materials sent, no follow-up in 7+ days
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage,
                       p.last_touch_ts, p.deal_amount_usd, p.next_step
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage = 'Materials sent'
                  AND (p.last_touch_ts IS NULL OR p.last_touch_ts < %s)
                ORDER BY p.deal_amount_usd DESC NULLS LAST, p.last_touch_ts ASC NULLS FIRST
            """, (now - follow_up_overdue_days * DAY,))
            materials_no_follow = list(cur.fetchall())

            # 7. Stale opportunities
            cur.execute("""
                SELECT o.product, o.stage, o.deal_amount_usd, o.next_step, o.updated_at,
                       p.name, p.email, f.name AS firm_name
                FROM crm_opportunities o
                JOIN crm_people p ON p.id = o.person_id
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE o.stage NOT IN ('Committed','Passed','Dormant')
                  AND o.updated_at < %s
                ORDER BY o.deal_amount_usd DESC NULLS LAST
                LIMIT 10
            """, (now - stale_opp_days * DAY,))
            stale_opps = list(cur.fetchall())

            # 8. Revisit-later prospects
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name,
                       p.revisit_later, p.stage, p.last_touch_ts, p.deal_amount_usd
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.revisit_later IS NOT NULL AND p.revisit_later != ''
                  AND p.stage NOT IN ('Committed','Passed')
                ORDER BY p.importance DESC NULLS LAST
                LIMIT 10
            """)
            revisit = list(cur.fetchall())

            # 9. Calls/meetings to book (next_step mentions scheduling)
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage,
                       p.next_step, p.last_touch_ts, p.deal_amount_usd
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage NOT IN ('Committed','Passed','Dormant')
                  AND (p.next_step ILIKE '%call%' OR p.next_step ILIKE '%meeting%'
                    OR p.next_step ILIKE '%zoom%' OR p.next_step ILIKE '%schedule%'
                    OR p.next_step ILIKE '%book%' OR p.next_step ILIKE '%catch up%')
                ORDER BY p.importance DESC NULLS LAST
                LIMIT 10
            """)
            calls_to_book = list(cur.fetchall())

            # 10. Not yet contacted: active prospects Joe has never sent anything to — but only
            # QUALIFIED ones (real mandate, amount, or priority). Excludes bare stray imports
            # (a name with nothing attached), which otherwise flood this section as noise.
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage, p.mandate,
                       p.investor_type, p.deal_amount_usd, p.importance, p.last_touch_ts
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.last_outbound_ts IS NULL
                  AND p.stage NOT IN ('Committed','Passed','Dormant')
                  AND (p.importance >= 4 OR p.deal_amount_usd IS NOT NULL
                       OR (p.mandate IS NOT NULL AND p.mandate != ''))
                ORDER BY p.importance DESC NULLS LAST, p.deal_amount_usd DESC NULLS LAST
                LIMIT 10
            """)
            not_yet_contacted = list(cur.fetchall())

            # 11. All active opportunities (for the by-investor/product breakdown, incl. non-stale)
            cur.execute("""
                SELECT o.product, o.stage, o.deal_amount_usd, o.next_step, o.updated_at,
                       p.name, p.email, f.name AS firm_name, p.investor_type
                FROM crm_opportunities o
                JOIN crm_people p ON p.id = o.person_id
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE o.stage NOT IN ('Committed','Passed','Dormant')
                ORDER BY COALESCE(p.name, f.name), o.deal_amount_usd DESC NULLS LAST
                LIMIT 30
            """)
            active_opportunities = list(cur.fetchall())

            # 12. Recent interaction summaries (last 7 days) for slipping analysis
            cur.execute("""
                SELECT i.direction, i.summary, i.ts, p.name, f.name AS firm_name, p.stage
                FROM crm_interactions i
                JOIN crm_people p ON p.id = i.person_id
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE i.ts > %s
                ORDER BY i.ts DESC LIMIT 25
            """, (now - 7 * DAY,))
            recent = list(cur.fetchall())

    return {
        "overdue_follow_ups": overdue,
        "unanswered_inbound": unanswered,
        "promised_items": promised,
        "going_cold": cold,
        "high_value_no_action": high_value_no_action,
        "materials_sent_no_follow": materials_no_follow,
        "stale_opportunities": stale_opps,
        "revisit_later": revisit,
        "calls_to_book": calls_to_book,
        "not_yet_contacted": not_yet_contacted,
        "active_opportunities": active_opportunities,
        "recent_interactions": recent,
    }


def _compact(signals: dict, now: int) -> str:
    def rows(key, fields):
        out = []
        for r in signals.get(key) or []:
            line = {f: r.get(f) for f in fields if r.get(f) is not None}
            ts = r.get("last_touch_ts") or r.get("updated_at") or r.get("ts")
            if ts:
                line["days_ago"] = _days_ago(ts, now)
            out.append(line)
        return out

    return json.dumps({
        "overdue_follow_ups": rows("overdue_follow_ups",
            ["name", "firm_name", "stage", "next_step", "deal_amount_usd", "importance"]),
        "unanswered_inbound": rows("unanswered_inbound",
            ["name", "firm_name", "stage", "subject", "summary"]),
        "promised_items": rows("promised_items",
            ["name", "firm_name", "stage", "next_step", "deal_amount_usd"]),
        "going_cold": rows("going_cold",
            ["name", "firm_name", "stage", "warmth", "deal_amount_usd", "importance"]),
        "high_value_no_action": rows("high_value_no_action",
            ["name", "firm_name", "stage", "deal_amount_usd", "importance"]),
        "materials_sent_no_follow": rows("materials_sent_no_follow",
            ["name", "firm_name", "next_step", "deal_amount_usd"]),
        "stale_opportunities": rows("stale_opportunities",
            ["name", "firm_name", "product", "stage", "deal_amount_usd", "next_step"]),
        "revisit_later": rows("revisit_later",
            ["name", "firm_name", "revisit_later", "deal_amount_usd"]),
        "calls_to_book": rows("calls_to_book",
            ["name", "firm_name", "stage", "next_step", "deal_amount_usd"]),
        "not_yet_contacted": rows("not_yet_contacted",
            ["name", "firm_name", "stage", "mandate", "investor_type", "deal_amount_usd", "importance"]),
        "active_opportunities": rows("active_opportunities",
            ["name", "firm_name", "investor_type", "product", "stage", "deal_amount_usd", "next_step"]),
        "recent_interactions": [
            {"direction": r.get("direction"), "summary": r.get("summary"),
             "name": r.get("name"), "firm": r.get("firm_name"), "stage": r.get("stage")}
            for r in (signals.get("recent_interactions") or [])
        ],
    }, default=str)


async def generate() -> str:
    now = int(_time.time())
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    signals = _gather(now)
    context = _compact(signals, now)
    resp = await claude.messages.create(
        model=MODEL,
        max_tokens=1400,
        system=DASHBOARD_PROMPT + crm_store.directives_prompt_block(),
        messages=[{"role": "user", "content": f"Today's date: {today}\n\nCRM signals (relative times in days_ago):\n{context}"}],
    )
    return resp.content[0].text.strip()
