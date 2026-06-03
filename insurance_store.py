import json
import os
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 5, dsn=os.environ["DATABASE_URL"]
        )
    return _pool


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS insurance_policies (
                    id              SERIAL PRIMARY KEY,
                    netuid          INTEGER NOT NULL,
                    trigger_type    TEXT NOT NULL,
                    coverage_amount REAL NOT NULL,
                    period_days     INTEGER NOT NULL,
                    premium         REAL NOT NULL,
                    risk_multiplier REAL NOT NULL,
                    signal_score    REAL NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'quoted',
                    wallet_address  TEXT,
                    quoted_ts       INTEGER NOT NULL,
                    activated_ts    INTEGER,
                    expires_ts      INTEGER NOT NULL,
                    payout_ts       INTEGER,
                    flagged         BOOLEAN DEFAULT FALSE,
                    flag_reason     TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lp_deposits (
                    id               SERIAL PRIMARY KEY,
                    depositor_id     TEXT NOT NULL,
                    amount_tao       REAL NOT NULL,
                    deposit_ts       INTEGER NOT NULL,
                    lockup_expires   INTEGER NOT NULL,
                    withdrawn        BOOLEAN DEFAULT FALSE,
                    withdrawn_ts     INTEGER,
                    withdrawn_amount REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS insurance_audit_log (
                    id          SERIAL PRIMARY KEY,
                    policy_id   INTEGER REFERENCES insurance_policies(id),
                    ts          INTEGER NOT NULL,
                    event_type  TEXT NOT NULL,
                    decision    TEXT NOT NULL,
                    reason      TEXT,
                    signals_json TEXT,
                    cycle_score REAL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_policies_netuid ON insurance_policies(netuid, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_policies_status ON insurance_policies(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_policy ON insurance_audit_log(policy_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deposits_depositor ON lp_deposits(depositor_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pool_transactions (
                    id           SERIAL PRIMARY KEY,
                    ts           INTEGER NOT NULL,
                    tx_type      TEXT NOT NULL,
                    amount_tao   REAL NOT NULL,
                    policy_id    INTEGER,
                    depositor_id TEXT,
                    notes        TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pool_tx_type ON pool_transactions(tx_type, ts)")


# --- policies ---

def create_policy(netuid, trigger_type, coverage_amount, period_days, premium,
                  risk_multiplier, signal_score, expires_ts, flagged=False, flag_reason=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO insurance_policies
                    (netuid, trigger_type, coverage_amount, period_days, premium,
                     risk_multiplier, signal_score, expires_ts, quoted_ts, flagged, flag_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (netuid, trigger_type, coverage_amount, period_days, premium,
                  risk_multiplier, signal_score, expires_ts, int(time.time()), flagged, flag_reason))
            return cur.fetchone()[0]


def activate_policy(policy_id, wallet_address):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE insurance_policies
                SET status = 'active', activated_ts = %s, wallet_address = %s
                WHERE id = %s AND status = 'quoted'
            """, (int(time.time()), wallet_address, policy_id))


def expire_policies():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE insurance_policies
                SET status = 'expired'
                WHERE status = 'active' AND expires_ts < %s
            """, (int(time.time()),))


def payout_policy(policy_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE insurance_policies
                SET status = 'paid_out', payout_ts = %s
                WHERE id = %s
            """, (int(time.time()), policy_id))


def get_active_policies():
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM insurance_policies WHERE status = 'active'
        """)


def get_policy(policy_id):
    with get_conn() as conn:
        rows = _rows(conn, "SELECT * FROM insurance_policies WHERE id = %s", (policy_id,))
        return rows[0] if rows else None


# --- pool state ---

def get_pool_state():
    with get_conn() as conn:
        rows = _rows(conn, """
            SELECT
                COALESCE(SUM(amount_tao), 0) AS total_pool_size
            FROM lp_deposits
            WHERE withdrawn = FALSE
        """)
        total_pool_size = rows[0]["total_pool_size"]

        rows2 = _rows(conn, """
            SELECT COALESCE(SUM(coverage_amount), 0) AS total_coverage
            FROM insurance_policies
            WHERE status = 'active'
        """)
        total_coverage = rows2[0]["total_coverage"]

        return {"total_pool_size": total_pool_size, "total_coverage": total_coverage}


def get_subnet_coverage(netuid):
    with get_conn() as conn:
        rows = _rows(conn, """
            SELECT COALESCE(SUM(coverage_amount), 0) AS total
            FROM insurance_policies
            WHERE netuid = %s AND status = 'active'
        """, (netuid,))
        return rows[0]["total"]


# --- LP deposits ---

def create_deposit(depositor_id, amount_tao):
    ts = int(time.time())
    lockup_expires = ts + 7 * 86400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lp_deposits (depositor_id, amount_tao, deposit_ts, lockup_expires)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (depositor_id, amount_tao, ts, lockup_expires))
            return cur.fetchone()[0]


def get_lp_deposits(depositor_id):
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM lp_deposits WHERE depositor_id = %s AND withdrawn = FALSE
        """, (depositor_id,))


def withdraw_deposit(deposit_id, amount):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE lp_deposits
                SET withdrawn = TRUE, withdrawn_ts = %s, withdrawn_amount = %s
                WHERE id = %s AND withdrawn = FALSE AND lockup_expires < %s
            """, (int(time.time()), amount, deposit_id, int(time.time())))


def get_lp_share(depositor_id):
    pool = get_pool_state()
    if pool["total_pool_size"] == 0:
        return 0.0
    with get_conn() as conn:
        rows = _rows(conn, """
            SELECT COALESCE(SUM(amount_tao), 0) AS total
            FROM lp_deposits
            WHERE depositor_id = %s AND withdrawn = FALSE
        """, (depositor_id,))
        depositor_total = rows[0]["total"]
        return depositor_total / pool["total_pool_size"]


# --- audit log ---

def log_audit(policy_id, event_type, decision, reason=None, signals=None, cycle_score=None):
    signals_json = json.dumps([
        {"model": s.model, "score": s.score, "confidence": s.confidence, "reason": s.reason}
        for s in signals
    ]) if signals else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO insurance_audit_log (policy_id, ts, event_type, decision, reason, signals_json, cycle_score)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (policy_id, int(time.time()), event_type, decision, reason, signals_json, cycle_score))


def get_audit_log(policy_id):
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM insurance_audit_log WHERE policy_id = %s ORDER BY ts ASC
        """, (policy_id,))


# --- pool transactions ---

def log_transaction(ts, tx_type, amount_tao, policy_id=None, depositor_id=None, notes=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pool_transactions (ts, tx_type, amount_tao, policy_id, depositor_id, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (ts, tx_type, amount_tao, policy_id, depositor_id, notes))


def get_transactions(tx_type=None, depositor_id=None, limit=50):
    conditions = []
    params = []
    if tx_type:
        conditions.append("tx_type = %s")
        params.append(tx_type)
    if depositor_id:
        conditions.append("depositor_id = %s")
        params.append(depositor_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    with get_conn() as conn:
        return _rows(conn, f"""
            SELECT * FROM pool_transactions {where} ORDER BY ts DESC LIMIT %s
        """, tuple(params))
