"""
CRM activity report — a PDF snapshot of what's happened and where the pipeline
stands: interaction volume, meetings/calls, pipeline stage distribution, and
prospective (active) transactions. Pure DB query + formatting, no LLM call —
the underlying data is already structured, same approach as crm_radar.

Note: the DB doesn't keep a historical log of stage transitions (only the
current stage per contact), so "stage progressions" here is an approximation —
contacts whose current stage is beyond New and who were touched within the
report window — not a true day-by-day transition history.
"""
from __future__ import annotations
import time

import psycopg2.extras

import store

DAY = 86400
MEETING_CHANNELS = ("phone", "video_call", "in_person")
ACTIVE_STAGES_EXCLUDE = ("Committed", "Passed", "Dormant")


def gather(days: int = 30) -> dict:
    now = int(time.time())
    since = now - days * DAY

    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Activity: interactions in window, by direction and sentiment
            cur.execute("""
                SELECT direction, sentiment, COUNT(*) AS n
                FROM crm_interactions WHERE ts >= %s
                GROUP BY direction, sentiment
            """, (since,))
            activity_rows = cur.fetchall()

            cur.execute("SELECT COUNT(*) AS n FROM crm_interactions WHERE ts >= %s", (since,))
            total_interactions = cur.fetchone()["n"]

            # Meetings/calls: interactions in window where the contact's own channel
            # is phone/video_call/in_person (per-interaction channel isn't tracked)
            cur.execute("""
                SELECT i.ts, i.subject, i.summary, p.name, p.email, p.contact_channel,
                       f.name AS firm_name
                FROM crm_interactions i
                JOIN crm_people p ON p.id = i.person_id
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE i.ts >= %s AND p.contact_channel = ANY(%s)
                ORDER BY i.ts DESC
            """, (since, list(MEETING_CHANNELS)))
            meetings = cur.fetchall()

            # Pipeline: current stage distribution across all active contacts
            cur.execute("""
                SELECT stage, COUNT(*) AS n
                FROM crm_people
                WHERE stage IS NOT NULL
                GROUP BY stage
            """)
            stage_counts = {r["stage"]: r["n"] for r in cur.fetchall()}

            # Stage progressions (approximate — see module docstring)
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.stage, p.updated_at
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage NOT IN ('New', 'Dormant', 'Passed')
                  AND p.updated_at >= %s
                ORDER BY p.updated_at DESC
                LIMIT 25
            """, (since,))
            progressions = cur.fetchall()

            # Prospective transactions: active opportunities, biggest/most-advanced first
            cur.execute("""
                SELECT o.product, o.stage, o.deal_amount_usd, o.next_step, o.updated_at,
                       COALESCE(p.name, p.email, f2.name) AS contact_or_firm,
                       COALESCE(pf.name, f2.name) AS firm_name
                FROM crm_opportunities o
                LEFT JOIN crm_people p ON p.id = o.person_id
                LEFT JOIN crm_firms pf ON pf.id = p.firm_id
                LEFT JOIN crm_firms f2 ON f2.id = o.firm_id
                WHERE o.stage NOT IN %s
                ORDER BY o.deal_amount_usd DESC NULLS LAST, o.updated_at DESC
                LIMIT 20
            """, (ACTIVE_STAGES_EXCLUDE,))
            transactions = cur.fetchall()

            cur.execute("""
                SELECT COUNT(*) AS n, COALESCE(SUM(deal_amount_usd), 0) AS total
                FROM crm_opportunities WHERE stage NOT IN %s
            """, (ACTIVE_STAGES_EXCLUDE,))
            pipeline_totals = cur.fetchone()

            cur.execute("SELECT COUNT(*) AS n FROM crm_people")
            total_contacts = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM crm_firms")
            total_firms = cur.fetchone()["n"]

    sentiment_counts: dict[str, int] = {}
    direction_counts: dict[str, int] = {}
    for r in activity_rows:
        if r["sentiment"]:
            sentiment_counts[r["sentiment"]] = sentiment_counts.get(r["sentiment"], 0) + r["n"]
        if r["direction"]:
            direction_counts[r["direction"]] = direction_counts.get(r["direction"], 0) + r["n"]

    return {
        "days": days,
        "total_interactions": total_interactions,
        "direction_counts": direction_counts,
        "sentiment_counts": sentiment_counts,
        "meetings": meetings,
        "stage_counts": stage_counts,
        "progressions": progressions,
        "transactions": transactions,
        "pipeline_count": pipeline_totals["n"],
        "pipeline_total_usd": float(pipeline_totals["total"] or 0),
        "total_contacts": total_contacts,
        "total_firms": total_firms,
    }
