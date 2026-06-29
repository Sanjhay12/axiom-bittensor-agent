"""
Phase 1 storage layer for the Cedar Ridge Inbox Agent.
Reuses the existing Postgres pool from store.py — adds crm_-prefixed tables
for relationship memory: firms, people, and the interactions that evidence them.
"""
from __future__ import annotations
import json
import time

import psycopg2.extras

import store


def init_crm_db():
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_firms (
                    id         SERIAL PRIMARY KEY,
                    name       TEXT UNIQUE NOT NULL,
                    created_at INTEGER NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_people (
                    id                SERIAL PRIMARY KEY,
                    email             TEXT UNIQUE NOT NULL,
                    name              TEXT,
                    firm_id           INTEGER REFERENCES crm_firms(id),
                    role              TEXT,
                    relationship_type TEXT,
                    mandate           TEXT,
                    stage             TEXT DEFAULT 'New',
                    last_touch_ts     INTEGER,
                    next_step         TEXT,
                    notes             TEXT,
                    sentiment         TEXT,
                    importance        INTEGER,
                    created_at        INTEGER NOT NULL,
                    updated_at        INTEGER NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_people_firm ON crm_people(firm_id)")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS pending_stage TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS pending_stage_reason TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'inbound'")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS enrichment TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS phone TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS contact_channel TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS deal_amount_usd REAL")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS manual_priority BOOLEAN DEFAULT FALSE")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_interactions (
                    id          SERIAL PRIMARY KEY,
                    person_id   INTEGER REFERENCES crm_people(id),
                    message_id  TEXT UNIQUE NOT NULL,
                    subject     TEXT,
                    direction   TEXT,
                    ts          INTEGER NOT NULL,
                    summary     TEXT,
                    entities    TEXT,
                    sentiment   TEXT,
                    importance  INTEGER,
                    raw_excerpt TEXT,
                    created_at  INTEGER NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_interactions_person ON crm_interactions(person_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_interactions_ts ON crm_interactions(ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_event_log (
                    id         SERIAL PRIMARY KEY,
                    ts         INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    details    TEXT,
                    created_at INTEGER NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_event_log_ts ON crm_event_log(ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_event_log_type ON crm_event_log(event_type)")


def get_or_create_firm(name: str | None) -> int | None:
    """Case-insensitive exact match first, then substring match either direction
    (so "Meridian" and "Meridian Family Office" from two different emails resolve
    to the same firm instead of fragmenting into duplicates), then create."""
    if not name or not name.strip():
        return None
    name = name.strip()
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM crm_firms WHERE LOWER(name) = LOWER(%s)", (name,))
            row = cur.fetchone()
            if row:
                return row["id"]
            cur.execute(
                "SELECT id FROM crm_firms WHERE %s ILIKE '%%' || name || '%%' OR name ILIKE %s LIMIT 1",
                (name, f"%{name}%"),
            )
            row = cur.fetchone()
            if row:
                return row["id"]
            cur.execute(
                "INSERT INTO crm_firms (name, created_at) VALUES (%s, %s) RETURNING id",
                (name, int(time.time())),
            )
            return cur.fetchone()["id"]


def upsert_person(extracted: dict, firm_id: int | None, ts: int, source: str = "inbound") -> tuple[int, bool]:
    """Insert or update a person record. New, non-null fields win; nulls never clobber existing data.

    Stage changes on an existing, already-staged contact are never applied silently —
    they're parked in pending_stage for a Telegram confirmation (see crm_ask.confirm_stage).
    Returns (person_id, is_new_person).
    """
    email_addr = (extracted.get("person_email") or "").strip().lower()
    if not email_addr:
        email_addr = f"unknown-{int(time.time())}"
    now = int(time.time())
    incoming_stage = extracted.get("stage")

    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, stage FROM crm_people WHERE email = %s", (email_addr,))
            row = cur.fetchone()

            if row:
                current_stage = row["stage"]
                stage_changed = (
                    incoming_stage and current_stage
                    and current_stage not in (None, "New") and incoming_stage != current_stage
                )
                new_stage = current_stage if stage_changed else (incoming_stage or current_stage)
                pending_stage = incoming_stage if stage_changed else None
                pending_reason = extracted.get("summary") if stage_changed else None

                cur.execute("""
                    UPDATE crm_people SET
                        name                 = COALESCE(%s, name),
                        firm_id              = COALESCE(firm_id, %s),
                        role                 = COALESCE(%s, role),
                        relationship_type    = COALESCE(%s, relationship_type),
                        mandate              = COALESCE(%s, mandate),
                        stage                = %s,
                        pending_stage        = %s,
                        pending_stage_reason = %s,
                        last_touch_ts        = %s,
                        next_step            = COALESCE(%s, next_step),
                        notes                = COALESCE(%s, notes),
                        sentiment            = COALESCE(%s, sentiment),
                        importance           = COALESCE(%s, importance),
                        phone                = COALESCE(%s, phone),
                        contact_channel      = COALESCE(%s, contact_channel),
                        deal_amount_usd      = COALESCE(%s, deal_amount_usd),
                        updated_at           = %s
                    WHERE id = %s
                """, (
                    extracted.get("person_name"), firm_id, extracted.get("role"),
                    extracted.get("relationship_type"), extracted.get("mandate"),
                    new_stage, pending_stage, pending_reason,
                    ts, extracted.get("next_step"),
                    extracted.get("notes"), extracted.get("sentiment"),
                    extracted.get("importance"),
                    extracted.get("phone"), extracted.get("contact_channel"), extracted.get("deal_amount_usd"),
                    now, row["id"],
                ))
                return row["id"], False

            cur.execute("""
                INSERT INTO crm_people (
                    email, name, firm_id, role, relationship_type, mandate,
                    stage, last_touch_ts, next_step, notes, sentiment, importance,
                    phone, contact_channel, deal_amount_usd, source,
                    created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                email_addr, extracted.get("person_name"), firm_id, extracted.get("role"),
                extracted.get("relationship_type"), extracted.get("mandate"),
                incoming_stage or "New", ts, extracted.get("next_step"),
                extracted.get("notes"), extracted.get("sentiment"),
                extracted.get("importance"),
                extracted.get("phone"), extracted.get("contact_channel"), extracted.get("deal_amount_usd"), source,
                now, now,
            ))
            return cur.fetchone()["id"], True


def confirm_pending_stage(person_id: int) -> str | None:
    """Applies a person's pending_stage as their actual stage. Returns the new stage, or None if nothing pending."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pending_stage FROM crm_people WHERE id = %s", (person_id,))
            row = cur.fetchone()
            if not row or not row["pending_stage"]:
                return None
            new_stage = row["pending_stage"]
            cur.execute("""
                UPDATE crm_people SET stage = %s, pending_stage = NULL, pending_stage_reason = NULL,
                       updated_at = %s WHERE id = %s
            """, (new_stage, int(time.time()), person_id))
            return new_stage


def reject_pending_stage(person_id: int):
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_people SET pending_stage = NULL, pending_stage_reason = NULL WHERE id = %s",
                (person_id,),
            )


def insert_interaction(person_id: int, message_id: str, subject: str, direction: str,
                        ts: int, extracted: dict, raw_excerpt: str) -> bool:
    """Returns False if this message_id was already recorded (dedupe)."""
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO crm_interactions (
                    person_id, message_id, subject, direction, ts, summary,
                    entities, sentiment, importance, raw_excerpt, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (message_id) DO NOTHING
            """, (
                person_id, message_id, subject, direction, ts,
                extracted.get("summary"), json.dumps(extracted.get("entities") or []),
                extracted.get("sentiment"), extracted.get("importance"),
                (raw_excerpt or "")[:2000], int(time.time()),
            ))
            return cur.rowcount > 0


def get_person(email_addr: str) -> dict | None:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM crm_people WHERE email = %s", (email_addr.strip().lower(),))
            return cur.fetchone()


def list_interactions(person_id: int) -> list[dict]:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM crm_interactions WHERE person_id = %s ORDER BY ts DESC",
                (person_id,),
            )
            return cur.fetchall()


TERMINAL_STAGES = ("Committed", "Passed", "Dormant")


def list_active_people() -> list[dict]:
    """All people not in a terminal pipeline stage, with firm name joined in."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, f.name AS firm_name
                FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage IS NULL OR p.stage NOT IN %s
                ORDER BY p.importance DESC NULLS LAST, p.last_touch_ts ASC
            """, (TERMINAL_STAGES,))
            return cur.fetchall()


def list_people_with_pending_stage() -> list[dict]:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, f.name AS firm_name FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.pending_stage IS NOT NULL
            """)
            return cur.fetchall()


def stage_counts() -> list[dict]:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(stage, 'New') AS stage, COUNT(*) AS cnt
                FROM crm_people GROUP BY stage ORDER BY cnt DESC
            """)
            return cur.fetchall()


def list_people_by_stage_detailed() -> list[dict]:
    """Every person with firm, deal size, and next step — for a pipeline view that's more
    than just counts. Ordered by stage, then importance within stage."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, COALESCE(p.stage, 'New') AS stage,
                       p.deal_amount_usd, p.next_step, p.importance
                FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                ORDER BY stage, p.importance DESC NULLS LAST
            """)
            return cur.fetchall()


def list_people_in_stage(stage_query: str) -> list[dict]:
    """Case-insensitive, substring-tolerant stage lookup — so "diligence" matches "Diligence"."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, f.name AS firm_name FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.stage ILIKE %s
                ORDER BY p.importance DESC NULLS LAST
            """, (f"%{stage_query.strip()}%",))
            return cur.fetchall()


def set_manual_priority(person_id: int, value: bool):
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_people SET manual_priority = %s, updated_at = %s WHERE id = %s",
                (value, int(time.time()), person_id),
            )


def list_manual_priority_people() -> list[dict]:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, f.name AS firm_name FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE p.manual_priority = TRUE
            """)
            return cur.fetchall()


def find_person(query: str) -> dict | None:
    """Looks up a person by exact email, or by name/firm substring (case-insensitive)."""
    query = (query or "").strip().lower()
    if not query:
        return None
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, f.name AS firm_name FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE LOWER(p.email) = %s
            """, (query,))
            row = cur.fetchone()
            if row:
                return row
            cur.execute("""
                SELECT p.*, f.name AS firm_name FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE LOWER(p.name) LIKE %s OR LOWER(f.name) LIKE %s
                ORDER BY p.importance DESC NULLS LAST LIMIT 1
            """, (f"%{query}%", f"%{query}%"))
            return cur.fetchone()


def all_people_brief_context() -> list[dict]:
    """Lightweight rows for the Ask Layer's context window: one row per person + their latest interaction."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.role, p.relationship_type,
                       p.mandate, p.stage, p.last_touch_ts, p.next_step, p.notes, p.importance,
                       (SELECT summary FROM crm_interactions i WHERE i.person_id = p.id ORDER BY ts DESC LIMIT 1) AS last_summary
                FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                ORDER BY p.importance DESC NULLS LAST, p.last_touch_ts DESC
                LIMIT 300
            """)
            return cur.fetchall()


def set_enrichment(person_id: int, data: dict):
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_people SET enrichment = %s, updated_at = %s WHERE id = %s",
                (json.dumps(data), int(time.time()), person_id),
            )


def log_event(event_type: str, details=None):
    """Write a single row to crm_event_log. Never raises — logging failures are silent."""
    now = int(time.time())
    payload = json.dumps(details) if isinstance(details, dict) else (str(details) if details else None)
    try:
        with store.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO crm_event_log (ts, event_type, details, created_at) VALUES (%s, %s, %s, %s)",
                    (now, event_type, payload, now),
                )
    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).warning(f"crm_store: failed to log event {event_type}: {e}")


def create_lead(name: str | None, firm_name: str | None, notes: str, source: str = "outbound_discovery") -> int:
    """Creates a low-confidence prospect record from outbound discovery (Phase 7) — no email yet."""
    firm_id = get_or_create_firm(firm_name)
    now = int(time.time())
    placeholder_email = f"lead-{int(time.time() * 1000)}@unknown"
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO crm_people (
                    email, name, firm_id, relationship_type, stage, notes,
                    importance, source, created_at, updated_at
                ) VALUES (%s,%s,%s,'lead','New',%s,1,%s,%s,%s)
                RETURNING id
            """, (placeholder_email, name, firm_id, notes, source, now, now))
            return cur.fetchone()["id"]
