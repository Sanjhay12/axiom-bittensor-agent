"""
Phase 1 storage layer for the Cedar Ridge Inbox Agent.
Reuses the existing Postgres pool from store.py — adds crm_-prefixed tables
for relationship memory: firms, people, and the interactions that evidence them.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime, timezone

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
            # Lets research (funding history, news) be saved and looked up for a fund/firm
            # that has no contact on file yet — a fund manager researching a prospective
            # LP's fund shouldn't need an actual person record first.
            cur.execute("ALTER TABLE crm_firms ADD COLUMN IF NOT EXISTS enrichment TEXT")

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
            # relationship context
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS investor_type TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS how_met TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS introduced_by TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS personal_notes TEXT")
            # warmth
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS warmth TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS last_replied_ts INTEGER")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS last_outbound_ts INTEGER")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS avg_response_days REAL")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS communication_style TEXT")
            # history
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS cares_about TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS passed_on TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS revisit_later TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS liked_products TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS objections TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS move_forward_conditions TEXT")
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS objection_profile JSONB DEFAULT '{}'::jsonb")
            # bucket -> resolved-at epoch seconds. Resolving a bucket does NOT remove it from
            # objection_profile — briefs/history still show it happened, just flagged handled.
            cur.execute("ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS objection_resolutions JSONB DEFAULT '{}'::jsonb")

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

            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_opportunities (
                    id             SERIAL PRIMARY KEY,
                    person_id      INTEGER REFERENCES crm_people(id) ON DELETE CASCADE,
                    product        TEXT NOT NULL,
                    stage          TEXT DEFAULT 'New',
                    deal_amount_usd REAL,
                    next_step      TEXT,
                    mandate        TEXT,
                    notes          TEXT,
                    created_at     INTEGER NOT NULL,
                    updated_at     INTEGER NOT NULL
                )
            """)
            # Objections are frequently product-specific (e.g. fee concern on Fund II but
            # not Fund I) — tracked per opportunity, separate from the contact-level
            # objections/objection_profile which capture general, not-product-tied concerns.
            cur.execute("ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS objections TEXT")
            cur.execute("ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS objection_profile JSONB DEFAULT '{}'::jsonb")
            cur.execute("ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS objection_resolutions JSONB DEFAULT '{}'::jsonb")
            # e.g. direct lending, distressed debt, mezzanine — set from product docs when the
            # opportunity is a credit/debt strategy; null for equity or other product types.
            cur.execute("ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS category TEXT")
            # An opportunity is anchored on the firm first (person_id is the specific contact
            # driving it, when one is known) — this lets a bulk "create this opportunity at
            # these N firms" request attach to the firm even when no individual contact was named.
            cur.execute("ALTER TABLE crm_opportunities ADD COLUMN IF NOT EXISTS firm_id INTEGER REFERENCES crm_firms(id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_opps_person ON crm_opportunities(person_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_opps_firm ON crm_opportunities(firm_id)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_processed_messages (
                    message_id   TEXT PRIMARY KEY,
                    processed_at INTEGER NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_name_flags (
                    id          SERIAL PRIMARY KEY,
                    person_a_id INTEGER NOT NULL REFERENCES crm_people(id) ON DELETE CASCADE,
                    person_b_id INTEGER NOT NULL REFERENCES crm_people(id) ON DELETE CASCADE,
                    message_id  TEXT,
                    created_at  INTEGER NOT NULL,
                    resolved_at INTEGER,
                    resolution  TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_name_flags_message_id ON crm_name_flags(message_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_name_flags_pending ON crm_name_flags(person_a_id, person_b_id) WHERE resolved_at IS NULL")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_firm_flags (
                    id          SERIAL PRIMARY KEY,
                    firm_a_id   INTEGER NOT NULL REFERENCES crm_firms(id) ON DELETE CASCADE,
                    firm_b_id   INTEGER NOT NULL REFERENCES crm_firms(id) ON DELETE CASCADE,
                    message_id  TEXT,
                    created_at  INTEGER NOT NULL,
                    resolved_at INTEGER,
                    resolution  TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_firm_flags_message_id ON crm_firm_flags(message_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_firm_flags_pending ON crm_firm_flags(firm_a_id, firm_b_id) WHERE resolved_at IS NULL")

            # Level 1 "directives": standing instructions the owner sets by email to steer the
            # agent's behaviour. Injected into the agent's prompts (dashboard, Q&A, drafting).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_directives (
                    id          SERIAL PRIMARY KEY,
                    directive   TEXT NOT NULL,
                    created_by  TEXT,
                    created_at  INTEGER NOT NULL,
                    active      BOOLEAN NOT NULL DEFAULT TRUE
                )
            """)

            # Bulk imports do a name/firm lookup per row — these keep get_or_create_firm
            # and the duplicate/name-conflict checks from degrading to full table scans
            # as the tables grow (was O(rows x table size) before these existed).
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_firms_name_lower ON crm_firms (LOWER(name))")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_firms_name_lower_pattern ON crm_firms (LOWER(name) text_pattern_ops)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_people_name_lower ON crm_people (LOWER(name))")


# ── Directives (Level 1: owner steers agent behaviour by email) ──────────────

def add_directive(directive: str, created_by: str | None) -> int:
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_directives (directive, created_by, created_at) VALUES (%s,%s,%s) RETURNING id",
                (directive.strip(), created_by, int(time.time())),
            )
            return cur.fetchone()[0]


def get_active_directives() -> list[dict]:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, directive, created_by FROM crm_directives WHERE active = TRUE ORDER BY id")
            return [dict(r) for r in cur.fetchall()]


def deactivate_directive(directive_id: int) -> bool:
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE crm_directives SET active = FALSE WHERE id = %s AND active = TRUE", (directive_id,))
            return cur.rowcount > 0


def directives_prompt_block() -> str:
    """Active directives formatted for injection into an LLM system prompt. Empty if none."""
    ds = get_active_directives()
    if not ds:
        return ""
    lines = "\n".join(f"- {d['directive']}" for d in ds)
    return (
        "\n\nSTANDING INSTRUCTIONS from the fund manager — these override defaults; follow them:\n"
        + lines
    )


_LEGAL_SUFFIX_RE = re.compile(
    r",?\s*(l\.?l\.?c\.?|inc\.?|incorporated|l\.?p\.?|ltd\.?|limited|corp\.?|corporation|"
    r"co\.?|company|plc|gmbh|pllc|p\.?c\.?)\s*$",
    re.IGNORECASE,
)


def _normalize_firm_name(name: str) -> str:
    """Strips a trailing legal-entity suffix (", LLC", ", Inc.", ", L.P.", etc.) so
    "Eagle Advisors" and "Eagle Advisors, LLC" compare equal — a bare word-boundary
    prefix match misses this because the comma before the suffix isn't a space, so
    "eagle advisors" was never recognized as a prefix of "eagle advisors, llc"."""
    stripped = _LEGAL_SUFFIX_RE.sub("", name.strip().lower()).strip()
    return stripped or name.strip().lower()


def get_or_create_firm(name: str | None) -> tuple[int, bool] | tuple[None, bool]:
    """Case-insensitive exact match first, then a legal-suffix-normalized match (so
    "Eagle Advisors" and "Eagle Advisors, LLC" resolve to the same firm), then a
    whole-word prefix match in either direction (so "Meridian" and "Meridian Family
    Office" from two different emails resolve to the same firm too). Both names must
    be at least 4 characters and the match must land on a word boundary, so short or
    generic text ("Unknown", "N/A", "LLC") can't silently merge into an unrelated firm.
    Otherwise creates a new firm. Returns (firm_id, is_new) — is_new tells the caller
    whether to run a fuzzy-duplicate check (see find_similar_firms) since only a
    freshly created firm needs one; an existing match was already deduplicated here."""
    if not name or not name.strip():
        return None, False
    name = name.strip()
    name_lower = name.lower()
    name_normalized = _normalize_firm_name(name)
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id FROM crm_firms WHERE LOWER(name) = %s", (name_lower,))
            row = cur.fetchone()
            if row:
                return row["id"], False

            if name_normalized != name_lower:
                cur.execute(
                    "SELECT id, name FROM crm_firms WHERE LOWER(name) = %s", (name_normalized,)
                )
                row = cur.fetchone()
                if row:
                    return row["id"], False
                cur.execute("SELECT id, name FROM crm_firms")
                for candidate in cur.fetchall():
                    if _normalize_firm_name(candidate["name"]) == name_normalized:
                        return candidate["id"], False

            if len(name) >= 4:
                # Direction 1: an existing (>=4 char) firm name is a word-boundary prefix
                # of `name` (e.g. existing "Meridian" + new "Meridian Family Office").
                # Every string that could satisfy this is one of name's own word-boundary
                # prefixes, so build those and do a single indexed lookup instead of
                # scanning every firm in the table.
                words = name_lower.split(" ")
                prefixes = []
                acc = ""
                for w in words[:-1]:
                    acc = f"{acc} {w}" if acc else w
                    if len(acc) >= 4:
                        prefixes.append(acc)
                if prefixes:
                    cur.execute(
                        "SELECT id FROM crm_firms WHERE LOWER(name) = ANY(%s) LIMIT 1",
                        (prefixes,),
                    )
                    row = cur.fetchone()
                    if row:
                        return row["id"], False

                # Direction 2: `name` is a word-boundary prefix of an existing, longer
                # firm name (e.g. new "Meridian" + existing "Meridian Family Office").
                cur.execute(
                    "SELECT id FROM crm_firms WHERE LOWER(name) LIKE %s LIMIT 1",
                    (name_lower + " %",),
                )
                row = cur.fetchone()
                if row:
                    return row["id"], False

            cur.execute(
                "INSERT INTO crm_firms (name, created_at) VALUES (%s, %s) RETURNING id",
                (name, int(time.time())),
            )
            return cur.fetchone()["id"], True


def find_similar_firms(name: str, firm_id: int) -> list[dict]:
    """Read-only fuzzy check run right after a NEW firm is created — looks for other
    existing firms whose name overlaps enough to plausibly be the same company (a
    substring match in either direction) so the caller can flag it for the user to
    confirm merge-or-keep-separate, rather than silently fragmenting into duplicates
    or silently merging something that might genuinely be a different company.
    Excludes any pair already dismissed via resolve_firm_flag."""
    if not name or not name.strip() or len(name.strip()) < 4:
        return []
    name_lower = name.strip().lower()
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, name FROM crm_firms
                WHERE id != %s
                AND (LOWER(name) LIKE %s OR %s LIKE '%%' || LOWER(name) || '%%')
                AND NOT EXISTS (
                    SELECT 1 FROM crm_firm_flags ff
                    WHERE ff.resolution = 'dismissed'
                    AND ((ff.firm_a_id = %s AND ff.firm_b_id = crm_firms.id)
                      OR (ff.firm_a_id = crm_firms.id AND ff.firm_b_id = %s))
                )
            """, (firm_id, f"%{name_lower}%", name_lower, firm_id, firm_id))
            return cur.fetchall()


def create_firm_flag(firm_a_id: int, firm_b_id: int, message_id: str | None) -> int | None:
    """Records a pending duplicate-firm flag tied to the email that reported it, so a
    later reply to that email can resolve it. Skips creating a new row if this exact
    (unordered) pair already has an unresolved flag outstanding."""
    now = int(time.time())
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM crm_firm_flags
                WHERE resolved_at IS NULL
                AND ((firm_a_id = %s AND firm_b_id = %s) OR (firm_a_id = %s AND firm_b_id = %s))
            """, (firm_a_id, firm_b_id, firm_b_id, firm_a_id))
            existing = cur.fetchone()
            if existing:
                return existing[0]
            cur.execute(
                "INSERT INTO crm_firm_flags (firm_a_id, firm_b_id, message_id, created_at) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (firm_a_id, firm_b_id, message_id, now),
            )
            return cur.fetchone()[0]


def get_pending_firm_flags_by_message_id(message_id: str) -> list[dict]:
    """Pending firm flags tied to the email thread the user is replying to."""
    if not message_id:
        return []
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ff.id AS flag_id, a.id AS a_id, a.name AS a_name, b.id AS b_id, b.name AS b_name
                FROM crm_firm_flags ff
                JOIN crm_firms a ON a.id = ff.firm_a_id
                JOIN crm_firms b ON b.id = ff.firm_b_id
                WHERE ff.message_id = %s AND ff.resolved_at IS NULL
            """, (message_id,))
            return cur.fetchall()


def get_all_pending_firm_flags() -> list[dict]:
    """All outstanding firm flags, regardless of thread — used as a fallback when a
    reply isn't threaded to a specific flag email but still clearly answers one."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ff.id AS flag_id, a.id AS a_id, a.name AS a_name, b.id AS b_id, b.name AS b_name
                FROM crm_firm_flags ff
                JOIN crm_firms a ON a.id = ff.firm_a_id
                JOIN crm_firms b ON b.id = ff.firm_b_id
                WHERE ff.resolved_at IS NULL
                ORDER BY ff.created_at DESC
            """)
            return cur.fetchall()


def resolve_firm_flag(flag_id: int, resolution: str):
    now = int(time.time())
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_firm_flags SET resolved_at = %s, resolution = %s WHERE id = %s",
                (now, resolution, flag_id),
            )


def merge_firms(keep_id: int, remove_id: int):
    """Folds remove_id into keep_id: reassigns every person/opportunity pointing at
    remove_id, deletes the remove_id row, and auto-resolves any other pending flags
    that referenced it (they'd otherwise point at a firm that no longer exists)."""
    if keep_id == remove_id:
        return
    now = int(time.time())
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE crm_people SET firm_id = %s WHERE firm_id = %s", (keep_id, remove_id))
            cur.execute("UPDATE crm_opportunities SET firm_id = %s WHERE firm_id = %s", (keep_id, remove_id))
            cur.execute(
                "UPDATE crm_firm_flags SET resolved_at = %s, resolution = %s "
                "WHERE resolved_at IS NULL AND (firm_a_id = %s OR firm_b_id = %s)",
                (now, "merged", remove_id, remove_id),
            )
            cur.execute("DELETE FROM crm_firms WHERE id = %s", (remove_id,))


def find_firm_by_name(name: str) -> dict | None:
    """Read-only firm lookup — never creates a firm (unlike get_or_create_firm). Exact match
    first, then a substring match in either direction (e.g. "Eagle Advisors" finds "Eagle
    Advisors, LLC", and vice versa), so shorthand names from an email still resolve without
    silently spawning a duplicate firm for a typo or partial name."""
    if not name or not name.strip():
        return None
    name = name.strip()
    name_lower = name.lower()
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM crm_firms WHERE LOWER(name) = %s", (name_lower,))
            row = cur.fetchone()
            if row:
                return row
            if len(name) >= 3:
                cur.execute(
                    "SELECT id, name FROM crm_firms WHERE LOWER(name) LIKE %s ORDER BY LENGTH(name) ASC LIMIT 1",
                    (f"%{name_lower}%",),
                )
                row = cur.fetchone()
                if row:
                    return row
    return None


def _resolve_last_touch_ts(extracted: dict, fallback_ts: int) -> int:
    """Real last-contact date Claude extracted (last_contact_date, ISO YYYY-MM-DD), falling
    back to the ingestion ts when absent/unparseable. Fixes last_touch reflecting when the
    email was forwarded to the agent inbox rather than when contact actually happened. A
    parsed date more than a day in the future (hallucinated) is ignored in favour of ts."""
    raw = extracted.get("last_contact_date")
    if raw:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(str(raw)[:10], fmt).replace(tzinfo=timezone.utc)
                secs = int(dt.timestamp())
                if secs <= fallback_ts + 86400:
                    return secs
                break
            except ValueError:
                continue
    return fallback_ts


def upsert_person(extracted: dict, firm_id: int | None, ts: int, source: str = "inbound") -> tuple[int, bool]:
    """Insert or update a person record. New, non-null fields win; nulls never clobber existing data.

    Stage changes on an existing, already-staged contact are never applied silently —
    they're parked in pending_stage for a Telegram confirmation (see crm_ask.confirm_stage).
    Returns (person_id, is_new_person).
    """
    email_addr = (extracted.get("person_email") or "").strip().lower()
    name = (extracted.get("person_name") or "").strip()
    now = int(time.time())
    incoming_stage = extracted.get("stage")
    last_touch = _resolve_last_touch_ts(extracted, ts)
    incoming_obj_profile = extracted.get("objection_profile")
    obj_profile_json = json.dumps({k: v for k, v in incoming_obj_profile.items() if v}) if incoming_obj_profile else None

    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            row = None
            if email_addr:
                cur.execute("SELECT id, stage FROM crm_people WHERE email = %s", (email_addr,))
                row = cur.fetchone()
            elif name:
                # No email in THIS interaction (common for casual call notes — "spoke with
                # Jane today...") — without this, every no-email mention would mint a new
                # synthetic-email row and silently fragment that contact's history. Match
                # by name+firm first (safest), then by name alone only if it's unambiguous.
                if firm_id is not None:
                    cur.execute(
                        "SELECT id, stage FROM crm_people WHERE LOWER(name) = LOWER(%s) AND firm_id = %s",
                        (name, firm_id),
                    )
                    row = cur.fetchone()
                if not row:
                    cur.execute("SELECT id, stage FROM crm_people WHERE LOWER(name) = LOWER(%s)", (name,))
                    candidates = cur.fetchall()
                    if len(candidates) == 1:
                        row = candidates[0]

            if not row and not email_addr:
                email_addr = f"unknown-{int(time.time())}"

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
                        name                   = COALESCE(%s, name),
                        firm_id                = COALESCE(firm_id, %s),
                        role                   = COALESCE(%s, role),
                        relationship_type      = COALESCE(%s, relationship_type),
                        mandate                = COALESCE(%s, mandate),
                        stage                  = %s,
                        pending_stage          = %s,
                        pending_stage_reason   = %s,
                        last_touch_ts          = %s,
                        next_step              = COALESCE(%s, next_step),
                        notes                  = COALESCE(%s, notes),
                        sentiment              = COALESCE(%s, sentiment),
                        importance             = COALESCE(%s, importance),
                        phone                  = COALESCE(%s, phone),
                        contact_channel        = COALESCE(%s, contact_channel),
                        deal_amount_usd        = COALESCE(%s, deal_amount_usd),
                        investor_type          = COALESCE(%s, investor_type),
                        how_met                = COALESCE(%s, how_met),
                        introduced_by          = COALESCE(%s, introduced_by),
                        personal_notes         = COALESCE(%s, personal_notes),
                        warmth                 = COALESCE(%s, warmth),
                        last_replied_ts        = COALESCE(%s, last_replied_ts),
                        last_outbound_ts       = COALESCE(%s, last_outbound_ts),
                        communication_style    = COALESCE(%s, communication_style),
                        cares_about            = COALESCE(%s, cares_about),
                        passed_on              = COALESCE(%s, passed_on),
                        revisit_later          = COALESCE(%s, revisit_later),
                        liked_products         = COALESCE(%s, liked_products),
                        objections             = COALESCE(%s, objections),
                        move_forward_conditions = COALESCE(%s, move_forward_conditions),
                        objection_profile      = CASE WHEN %s IS NOT NULL THEN COALESCE(objection_profile, '{}'::jsonb) || %s::jsonb ELSE objection_profile END,
                        updated_at             = %s
                    WHERE id = %s
                """, (
                    extracted.get("person_name"), firm_id, extracted.get("role"),
                    extracted.get("relationship_type"), extracted.get("mandate"),
                    new_stage, pending_stage, pending_reason,
                    last_touch, extracted.get("next_step"),
                    extracted.get("notes"), extracted.get("sentiment"),
                    extracted.get("importance"),
                    extracted.get("phone"), extracted.get("contact_channel"), extracted.get("deal_amount_usd"),
                    extracted.get("investor_type"), extracted.get("how_met"), extracted.get("introduced_by"),
                    extracted.get("personal_notes"), extracted.get("warmth"),
                    extracted.get("last_replied_ts"), extracted.get("last_outbound_ts"),
                    extracted.get("communication_style"), extracted.get("cares_about"),
                    extracted.get("passed_on"), extracted.get("revisit_later"),
                    extracted.get("liked_products"), extracted.get("objections"),
                    extracted.get("move_forward_conditions"),
                    obj_profile_json, obj_profile_json,
                    now, row["id"],
                ))
                return row["id"], False

            cur.execute("""
                INSERT INTO crm_people (
                    email, name, firm_id, role, relationship_type, mandate,
                    stage, last_touch_ts, next_step, notes, sentiment, importance,
                    phone, contact_channel, deal_amount_usd, source,
                    investor_type, how_met, introduced_by, personal_notes, warmth,
                    last_replied_ts, last_outbound_ts, communication_style,
                    cares_about, passed_on, revisit_later, liked_products, objections,
                    move_forward_conditions, objection_profile, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                email_addr, extracted.get("person_name"), firm_id, extracted.get("role"),
                extracted.get("relationship_type"), extracted.get("mandate"),
                incoming_stage or "New", last_touch, extracted.get("next_step"),
                extracted.get("notes"), extracted.get("sentiment"), extracted.get("importance"),
                extracted.get("phone"), extracted.get("contact_channel"), extracted.get("deal_amount_usd"), source,
                extracted.get("investor_type"), extracted.get("how_met"), extracted.get("introduced_by"),
                extracted.get("personal_notes"), extracted.get("warmth"),
                extracted.get("last_replied_ts"), extracted.get("last_outbound_ts"),
                extracted.get("communication_style"), extracted.get("cares_about"),
                extracted.get("passed_on"), extracted.get("revisit_later"),
                extracted.get("liked_products"), extracted.get("objections"),
                extracted.get("move_forward_conditions"), obj_profile_json, now, now,
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
    """Returns False if this message_id was already recorded (dedupe).

    For inbound relationship mail, the interaction's ts is the real contact date Claude
    extracted (not when it was forwarded to the inbox). Outbound (Joe's own replies) has
    no extracted date, so it falls back to the passed ts (= now), which is correct."""
    interaction_ts = _resolve_last_touch_ts(extracted, ts) if direction == "inbound" else ts
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO crm_interactions (
                    person_id, message_id, subject, direction, ts, summary,
                    entities, sentiment, importance, raw_excerpt, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (message_id) DO NOTHING
            """, (
                person_id, message_id, subject, direction, interaction_ts,
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


def list_interactions_for_people(person_ids: list[int]) -> dict[int, list[dict]]:
    """Batched version of list_interactions for scoring many contacts at once (e.g.
    rank_active_people) — one round trip instead of one per person."""
    if not person_ids:
        return {}
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM crm_interactions WHERE person_id = ANY(%s) ORDER BY ts DESC",
                (person_ids,),
            )
            by_person: dict[int, list[dict]] = {}
            for row in cur.fetchall():
                by_person.setdefault(row["person_id"], []).append(row)
            return by_person


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
    """Lightweight rows for the Ask Layer's context window: one row per person + their latest
    interaction + every one of their opportunities (a contact can have several — free-form
    questions need to see all of them, not just the single contact-level mandate field).
    Includes every contact-level field (phone, warmth, personal notes, enrichment, etc.) so
    free-form questions like "what's Jane's phone number" or "what did we find on Acme's
    funding" can be answered without needing the exact whois/score command syntax."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.name, p.email, f.name AS firm_name, p.role, p.relationship_type,
                       p.mandate, p.stage, p.last_touch_ts, p.next_step, p.notes, p.importance,
                       p.phone, p.contact_channel, p.investor_type, p.how_met, p.introduced_by,
                       p.warmth, p.communication_style, p.cares_about, p.passed_on,
                       p.revisit_later, p.liked_products, p.objections, p.objection_profile,
                       p.objection_resolutions, p.move_forward_conditions, p.personal_notes,
                       p.manual_priority, p.pending_stage, p.deal_amount_usd, p.enrichment,
                       (SELECT summary FROM crm_interactions i WHERE i.person_id = p.id ORDER BY ts DESC LIMIT 1) AS last_summary,
                       (
                           SELECT json_agg(json_build_object(
                               'product', o.product, 'category', o.category, 'stage', o.stage,
                               'deal_amount_usd', o.deal_amount_usd,
                               'next_step', o.next_step, 'notes', o.notes,
                               'objections', o.objections, 'objection_profile', o.objection_profile,
                               'objection_resolutions', o.objection_resolutions
                           ))
                           FROM crm_opportunities o WHERE o.person_id = p.id
                       ) AS opportunities
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


def set_firm_enrichment(firm_id: int, data: dict):
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_firms SET enrichment = %s WHERE id = %s",
                (json.dumps(data), firm_id),
            )


def get_firm(firm_id: int) -> dict | None:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, name, enrichment FROM crm_firms WHERE id = %s", (firm_id,))
            return cur.fetchone()


def upsert_opportunity(person_id: int | None, product: str, stage: str | None = None,
                       deal_amount_usd: float | None = None, next_step: str | None = None,
                       mandate: str | None = None, notes: str | None = None,
                       objections: str | None = None, objection_profile: dict | None = None,
                       category: str | None = None, firm_id: int | None = None) -> int:
    """Insert or update an opportunity, anchored on the firm first — person_id is the specific
    contact driving it when one is known, but a firm-level opportunity (person_id=None,
    firm_id given) is valid, e.g. from a bulk "create this opportunity at these firms" request
    that doesn't name individual contacts. When person_id is given and firm_id isn't, firm_id is
    filled in from that person's own firm. Matches on person_id + product when a contact is
    given, otherwise on firm_id + product.
    objection_profile buckets merge additively (like the contact-level profile) rather than
    overwriting, since different emails may surface different concerns about the same deal.
    category is the credit-strategy type (direct lending, distressed debt, etc.) when applicable."""
    if person_id is None and firm_id is None:
        raise ValueError("upsert_opportunity requires person_id or firm_id")
    now = int(time.time())
    obj_profile_json = json.dumps({k: v for k, v in objection_profile.items() if v}) if objection_profile else None
    product_lower = product.strip().lower()
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if person_id is not None and firm_id is None:
                cur.execute("SELECT firm_id FROM crm_people WHERE id = %s", (person_id,))
                row = cur.fetchone()
                firm_id = row["firm_id"] if row else None

            # Fuzzy-match against this contact's (or firm's, when there's no specific
            # contact) own opportunities — a small, safe search space, unlike the global
            # firm-name case — "Fund II" and "Cedar Ridge Fund II" should resolve to the
            # same row, not fragment into duplicates the way a strict exact match would.
            if person_id is not None:
                cur.execute("SELECT id, product FROM crm_opportunities WHERE person_id = %s", (person_id,))
            else:
                cur.execute(
                    "SELECT id, product FROM crm_opportunities WHERE firm_id = %s AND person_id IS NULL",
                    (firm_id,),
                )
            existing = cur.fetchall()
            row = next((e for e in existing if e["product"].lower() == product_lower), None)
            if not row:
                row = next(
                    (e for e in existing if product_lower in e["product"].lower() or e["product"].lower() in product_lower),
                    None,
                )
            if row:
                cur.execute("""
                    UPDATE crm_opportunities SET
                        firm_id           = COALESCE(%s, firm_id),
                        stage             = COALESCE(%s, stage),
                        deal_amount_usd   = COALESCE(%s, deal_amount_usd),
                        next_step         = COALESCE(%s, next_step),
                        mandate           = COALESCE(%s, mandate),
                        notes             = COALESCE(%s, notes),
                        objections        = COALESCE(%s, objections),
                        objection_profile = CASE WHEN %s IS NOT NULL THEN COALESCE(objection_profile, '{}'::jsonb) || %s::jsonb ELSE objection_profile END,
                        category          = COALESCE(%s, category),
                        updated_at        = %s
                    WHERE id = %s
                """, (firm_id, stage, deal_amount_usd, next_step, mandate, notes, objections,
                      obj_profile_json, obj_profile_json, category, now, row["id"]))
                return row["id"]
            cur.execute("""
                INSERT INTO crm_opportunities
                    (person_id, firm_id, product, stage, deal_amount_usd, next_step, mandate, notes,
                     objections, objection_profile, category, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (person_id, firm_id, product.strip(), stage or "New", deal_amount_usd, next_step, mandate, notes,
                  objections, obj_profile_json or '{}', category, now, now))
            return cur.fetchone()["id"]


def list_opportunities(person_id: int) -> list[dict]:
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM crm_opportunities WHERE person_id = %s ORDER BY updated_at DESC",
                (person_id,),
            )
            return cur.fetchall()


def list_opportunities_for_people(person_ids: list[int]) -> dict[int, list[dict]]:
    """Batched version of list_opportunities for scanning many contacts at once (e.g. the
    daily radar) — one round trip instead of one per person."""
    if not person_ids:
        return {}
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT o.*, f.name AS firm_name FROM crm_opportunities o
                LEFT JOIN crm_firms f ON f.id = o.firm_id
                WHERE o.person_id = ANY(%s) ORDER BY o.updated_at DESC
            """, (person_ids,))
            by_person: dict[int, list[dict]] = {}
            for row in cur.fetchall():
                by_person.setdefault(row["person_id"], []).append(row)
            return by_person


def find_duplicate_by_name_and_firm(name: str, firm_id: int | None) -> dict | None:
    """Returns an existing person with the same name at the same firm, or None."""
    if not name or not name.strip():
        return None
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if firm_id is not None:
                cur.execute("""
                    SELECT p.*, f.name AS firm_name FROM crm_people p
                    LEFT JOIN crm_firms f ON f.id = p.firm_id
                    WHERE LOWER(p.name) = LOWER(%s) AND p.firm_id = %s
                """, (name.strip(), firm_id))
            else:
                cur.execute("""
                    SELECT p.*, f.name AS firm_name FROM crm_people p
                    LEFT JOIN crm_firms f ON f.id = p.firm_id
                    WHERE LOWER(p.name) = LOWER(%s) AND p.firm_id IS NULL
                """, (name.strip(),))
            return cur.fetchone()


def find_people_by_name(name: str) -> list[dict]:
    """Returns every person with this exact (case-insensitive) name — used to disambiguate
    a standalone merge/dismiss request naming two people rather than replying to a flag."""
    if not name or not name.strip():
        return []
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, f.name AS firm_name
                FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE LOWER(p.name) = LOWER(%s)
            """, (name.strip(),))
            return cur.fetchall()


def find_people_by_firm(firm_query: str) -> list[dict]:
    """Returns all people at a firm matching the query (case-insensitive substring)."""
    if not firm_query or not firm_query.strip():
        return []
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.*, f.name AS firm_name
                FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE LOWER(f.name) LIKE %s
                ORDER BY p.importance DESC NULLS LAST
            """, (f"%{firm_query.strip().lower()}%",))
            return cur.fetchall()


def find_name_conflicts(name: str, person_id: int) -> list[dict]:
    """Returns other people with the same name at a different firm — excluding any
    pair the user has already told the agent are different people (see merge_people /
    resolve_name_flag), so a dismissed flag doesn't just come back on the next import."""
    if not name or not name.strip():
        return []
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.id, p.name, p.email, f.name AS firm_name
                FROM crm_people p
                LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE LOWER(p.name) = LOWER(%s) AND p.id != %s
                AND NOT EXISTS (
                    SELECT 1 FROM crm_name_flags nf
                    WHERE nf.resolution = 'dismissed'
                    AND ((nf.person_a_id = %s AND nf.person_b_id = p.id)
                      OR (nf.person_a_id = p.id AND nf.person_b_id = %s))
                )
            """, (name.strip(), person_id, person_id, person_id))
            return cur.fetchall()


def create_name_flag(person_a_id: int, person_b_id: int, message_id: str | None) -> int | None:
    """Records a pending duplicate-name flag tied to the email that reported it, so a
    later reply to that email can resolve it. Skips creating a new row if this exact
    (unordered) pair already has an unresolved flag outstanding."""
    now = int(time.time())
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM crm_name_flags
                WHERE resolved_at IS NULL
                AND ((person_a_id = %s AND person_b_id = %s) OR (person_a_id = %s AND person_b_id = %s))
            """, (person_a_id, person_b_id, person_b_id, person_a_id))
            existing = cur.fetchone()
            if existing:
                return existing[0]
            cur.execute(
                "INSERT INTO crm_name_flags (person_a_id, person_b_id, message_id, created_at) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (person_a_id, person_b_id, message_id, now),
            )
            return cur.fetchone()[0]


def get_pending_flags_by_message_id(message_id: str) -> list[dict]:
    """Pending flags tied to the email thread the user is replying to."""
    if not message_id:
        return []
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT nf.id AS flag_id,
                       a.id AS a_id, a.name AS a_name, a.email AS a_email, fa.name AS a_firm,
                       b.id AS b_id, b.name AS b_name, b.email AS b_email, fb.name AS b_firm
                FROM crm_name_flags nf
                JOIN crm_people a ON a.id = nf.person_a_id
                JOIN crm_people b ON b.id = nf.person_b_id
                LEFT JOIN crm_firms fa ON fa.id = a.firm_id
                LEFT JOIN crm_firms fb ON fb.id = b.firm_id
                WHERE nf.message_id = %s AND nf.resolved_at IS NULL
            """, (message_id,))
            return cur.fetchall()


def get_all_pending_flags() -> list[dict]:
    """All outstanding flags, regardless of thread — used as a fallback when a reply
    isn't threaded to a specific flag email but still clearly answers one."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT nf.id AS flag_id,
                       a.id AS a_id, a.name AS a_name, a.email AS a_email, fa.name AS a_firm,
                       b.id AS b_id, b.name AS b_name, b.email AS b_email, fb.name AS b_firm
                FROM crm_name_flags nf
                JOIN crm_people a ON a.id = nf.person_a_id
                JOIN crm_people b ON b.id = nf.person_b_id
                LEFT JOIN crm_firms fa ON fa.id = a.firm_id
                LEFT JOIN crm_firms fb ON fb.id = b.firm_id
                WHERE nf.resolved_at IS NULL
                ORDER BY nf.created_at DESC
            """)
            return cur.fetchall()


def resolve_flag(flag_id: int, resolution: str):
    now = int(time.time())
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_name_flags SET resolved_at = %s, resolution = %s WHERE id = %s",
                (now, resolution, flag_id),
            )


def merge_people(keep_id: int, remove_id: int):
    """Folds remove_id into keep_id: reassigns its interactions/opportunities, fills
    any blank fields on keep_id from remove_id's data (never overwrites a non-null
    field), deletes the remove_id row, and auto-resolves any other pending flags that
    referenced it (they'd otherwise point at a person that no longer exists)."""
    if keep_id == remove_id:
        return
    now = int(time.time())
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("UPDATE crm_interactions SET person_id = %s WHERE person_id = %s", (keep_id, remove_id))
            cur.execute(
                "UPDATE crm_opportunities SET person_id = %s WHERE person_id = %s RETURNING id",
                (keep_id, remove_id),
            )
            reassigned_opp_ids = [r["id"] for r in cur.fetchall()]

            cur.execute("SELECT * FROM crm_people WHERE id = %s", (remove_id,))
            loser = cur.fetchone()
            cur.execute("SELECT * FROM crm_people WHERE id = %s", (keep_id,))
            winner = cur.fetchone()
            if loser and winner:
                fillable = [
                    "phone", "role", "relationship_type", "mandate", "next_step", "notes",
                    "sentiment", "importance", "personal_notes", "communication_style",
                    "cares_about", "passed_on", "revisit_later", "liked_products",
                    "objections", "move_forward_conditions", "how_met", "introduced_by",
                    "investor_type", "contact_channel", "warmth",
                ]
                updates = {k: loser[k] for k in fillable if k in loser and winner.get(k) is None and loser.get(k) is not None}
                if updates:
                    fields_sql = ", ".join(f"{k} = %s" for k in updates)
                    cur.execute(
                        f"UPDATE crm_people SET {fields_sql}, updated_at = %s WHERE id = %s",
                        list(updates.values()) + [now, keep_id],
                    )
                final_firm_id = winner.get("firm_id")
                if final_firm_id is None and loser.get("firm_id") is not None:
                    final_firm_id = loser["firm_id"]
                    cur.execute("UPDATE crm_people SET firm_id = %s WHERE id = %s", (final_firm_id, keep_id))

                # The opportunities just reassigned from remove_id may still carry ITS
                # old firm_id (e.g. logged before the two records were recognized as the
                # same person at a different firm) — bring them onto the now-current firm
                # so briefs/radar don't show a stale firm name next to the correct one.
                if final_firm_id is not None and reassigned_opp_ids:
                    cur.execute(
                        "UPDATE crm_opportunities SET firm_id = %s WHERE id = ANY(%s)",
                        (final_firm_id, reassigned_opp_ids),
                    )

            cur.execute(
                "UPDATE crm_name_flags SET resolved_at = %s, resolution = 'merged' "
                "WHERE resolved_at IS NULL AND (person_a_id = %s OR person_b_id = %s)",
                (now, remove_id, remove_id),
            )
            cur.execute("DELETE FROM crm_people WHERE id = %s", (remove_id,))


def claim_message(message_id: str) -> bool:
    """Atomically marks a message_id as processed. Returns True if this call is the
    first to claim it (safe to process), False if it was already claimed — by an
    earlier poll cycle, a retry, or a second poller instance hitting the same mailbox.
    Relying on IMAP's \\Seen flag alone isn't enough to prevent double-processing
    (flag changes can race across connections), so this is the actual dedup guard."""
    now = int(time.time())
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crm_processed_messages (message_id, processed_at) VALUES (%s, %s) "
                "ON CONFLICT (message_id) DO NOTHING",
                (message_id, now),
            )
            return cur.rowcount > 0


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


def clear_next_step(person_id: int):
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE crm_people SET next_step = NULL, updated_at = %s WHERE id = %s",
                (int(time.time()), person_id),
            )


def merge_objection_profile(person_id: int, new_profile: dict):
    """Merge new objection buckets into the existing profile without overwriting unrelated buckets."""
    clean = {k: v for k, v in new_profile.items() if v}
    if not clean:
        return
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE crm_people
                SET objection_profile = COALESCE(objection_profile, '{}'::jsonb) || %s::jsonb,
                    updated_at = %s
                WHERE id = %s
            """, (json.dumps(clean), int(time.time()), person_id))


OBJECTION_LABELS = {
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


def _mark_objection_bucket(bucket: str, person_id: int | None, opportunity_id: int | None, erase: bool) -> str | None:
    """Shared implementation for resolve_objection/erase_objection — same logic applies to
    either a contact's or an opportunity's objection_profile, just a different table.
    Resolving leaves the bucket in objection_profile (so briefs/history still show it was
    raised) and only stamps objection_resolutions; erasing removes the bucket entirely,
    for objections that were logged in error. Returns the bucket's text, or None if that
    bucket doesn't exist on this record."""
    if (person_id is None) == (opportunity_id is None):
        raise ValueError("_mark_objection_bucket requires exactly one of person_id or opportunity_id")
    table = "crm_people" if person_id is not None else "crm_opportunities"
    row_id = person_id if person_id is not None else opportunity_id
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT objection_profile, objection_resolutions FROM {table} WHERE id = %s", (row_id,))
            row = cur.fetchone()
            if not row:
                return None
            profile = row.get("objection_profile") or {}
            if not profile.get(bucket):
                return None
            resolved_text = profile[bucket]
            resolutions = row.get("objection_resolutions") or {}
            now = int(time.time())
            if erase:
                profile = {k: v for k, v in profile.items() if k != bucket}
                resolutions = {k: v for k, v in resolutions.items() if k != bucket}
            else:
                resolutions = {**resolutions, bucket: now}
            cur.execute(
                f"UPDATE {table} SET objection_profile = %s, objection_resolutions = %s, updated_at = %s WHERE id = %s",
                (json.dumps(profile), json.dumps(resolutions), now, row_id),
            )
            return resolved_text


def resolve_objection(bucket: str, person_id: int | None = None, opportunity_id: int | None = None) -> str | None:
    """Marks one objection bucket resolved without deleting it. Exactly one of
    person_id/opportunity_id must be given. Returns the objection's text, or None if
    that bucket isn't present on this record."""
    return _mark_objection_bucket(bucket, person_id, opportunity_id, erase=False)


def erase_objection(bucket: str, person_id: int | None = None, opportunity_id: int | None = None) -> str | None:
    """Deletes one objection bucket entirely — use for objections logged in error, not
    ones that were simply handled (use resolve_objection for that). Returns the removed
    text, or None if that bucket isn't present on this record."""
    return _mark_objection_bucket(bucket, person_id, opportunity_id, erase=True)


def create_lead(name: str | None, firm_name: str | None, notes: str, source: str = "outbound_discovery") -> int:
    """Creates a low-confidence prospect record from outbound discovery (Phase 7) — no email yet."""
    firm_id, _ = get_or_create_firm(firm_name)
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
