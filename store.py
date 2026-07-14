from __future__ import annotations
import json
import os
import secrets
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        # Max bumped from 5: a bulk contact import schedules one concurrent
        # crm_enrich.enrich_person() task per new contact via asyncio.create_task,
        # each needing its own connection — a large import could burst well past 5
        # and hit PoolError (which isn't caught, so it'd break that enrichment run).
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 20, dsn=os.environ["DATABASE_URL"]
        )
    return _pool


def _healthy_conn(pool):
    """Return a live pooled connection, transparently replacing one the server has dropped.
    Railway's Postgres proxy closes idle connections; psycopg2's pool otherwise hands the dead
    one straight back, causing 'connection already closed' on first use. A cheap SELECT 1 probes
    liveness and a stale connection is discarded (closed) and replaced with a fresh one."""
    conn = None
    for _ in range(3):
        conn = pool.getconn()
        try:
            if conn.closed:
                raise psycopg2.OperationalError("stale pooled connection")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()  # end the probe's implicit transaction; hand back a clean connection
            return conn
        except psycopg2.Error:
            try:
                pool.putconn(conn, close=True)  # drop the dead one instead of recycling it
            except Exception:
                pass
            conn = None
    # Couldn't validate one in a few tries — best effort, let the caller proceed.
    return conn if conn is not None else pool.getconn()


@contextmanager
def get_conn():
    pool = _get_pool()
    conn = _healthy_conn(pool)
    broken = False
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            broken = True  # rollback failed (connection dropped mid-use) — don't recycle it
        raise
    finally:
        try:
            pool.putconn(conn, close=broken)
        except Exception:
            pass


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subnet_snapshots (
                    id                 SERIAL PRIMARY KEY,
                    ts                 INTEGER NOT NULL,
                    netuid             INTEGER NOT NULL,
                    neuron_count       INTEGER,
                    max_neurons        INTEGER,
                    reg_cost_tao       REAL,
                    total_stake_tao    REAL,
                    total_emission_tao REAL,
                    validator_count    INTEGER,
                    miner_count        INTEGER,
                    alpha_price_tao    REAL,
                    tempo              INTEGER,
                    immunity_period    INTEGER
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_subnet_ts ON subnet_snapshots(netuid, ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS validator_snapshots (
                    id              SERIAL PRIMARY KEY,
                    ts              INTEGER NOT NULL,
                    netuid          INTEGER NOT NULL,
                    uid             INTEGER NOT NULL,
                    hotkey          TEXT,
                    stake_tao       REAL,
                    stake_delta_tao REAL,
                    dividends       REAL,
                    vtrust          REAL,
                    consensus       REAL,
                    emission_tao    REAL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_validator_ts ON validator_snapshots(netuid, ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS churn_events (
                    id                  SERIAL PRIMARY KEY,
                    ts                  INTEGER NOT NULL,
                    netuid              INTEGER NOT NULL,
                    registered_count    INTEGER,
                    deregistered_count  INTEGER,
                    churn_rate          REAL,
                    new_uids            TEXT,
                    lost_uids           TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_churn_ts ON churn_events(netuid, ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS network_snapshots (
                    id                  SERIAL PRIMARY KEY,
                    ts                  INTEGER NOT NULL,
                    block               INTEGER,
                    total_issuance_tao  REAL,
                    total_stake_tao     REAL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id          SERIAL PRIMARY KEY,
                    ts          INTEGER NOT NULL,
                    price_usd   REAL,
                    change_24h  REAL,
                    market_cap  REAL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS miner_snapshots (
                    id           SERIAL PRIMARY KEY,
                    ts           INTEGER NOT NULL,
                    netuid       INTEGER NOT NULL,
                    uid          INTEGER NOT NULL,
                    hotkey       TEXT,
                    incentive    REAL,
                    consensus    REAL,
                    emission_tao REAL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_miner_ts ON miner_snapshots(netuid, ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS github_activity (
                    id            SERIAL PRIMARY KEY,
                    ts            INTEGER NOT NULL,
                    netuid        INTEGER NOT NULL,
                    repo_url      TEXT,
                    commits_7d    INTEGER,
                    commits_30d   INTEGER,
                    open_prs      INTEGER,
                    open_issues   INTEGER,
                    stars         INTEGER,
                    forks         INTEGER
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_github_ts ON github_activity(netuid, ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS weight_snapshots (
                    id              SERIAL PRIMARY KEY,
                    ts              INTEGER NOT NULL,
                    netuid          INTEGER NOT NULL,
                    validator_uid   INTEGER NOT NULL,
                    weights_json    TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_weight_ts ON weight_snapshots(netuid, ts)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_memory (
                    id   INTEGER PRIMARY KEY,
                    data TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS relay_queries (
                    query_id     BIGINT PRIMARY KEY,
                    input_text   TEXT NOT NULL,
                    result_text  TEXT,
                    received_at  INTEGER NOT NULL,
                    fulfilled_at INTEGER
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    wallet      TEXT PRIMARY KEY,
                    tx_hash     TEXT NOT NULL,
                    created_at  INTEGER NOT NULL,
                    expires_at  INTEGER NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS access_codes (
                    code        TEXT PRIMARY KEY,
                    wallet      TEXT NOT NULL,
                    telegram_id BIGINT,
                    created_at  INTEGER NOT NULL,
                    used_at     INTEGER
                )
            """)


def _rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _one(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def insert_subnet_snapshot(ts: int, netuid: int, data: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subnet_snapshots
                    (ts, netuid, neuron_count, max_neurons, reg_cost_tao,
                     total_stake_tao, total_emission_tao,
                     validator_count, miner_count, alpha_price_tao,
                     tempo, immunity_period)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                ts, netuid,
                data.get("neuron_count"), data.get("max_neurons"),
                data.get("reg_cost_tao"),
                data.get("total_stake_tao"), data.get("total_emission_tao"),
                data.get("validator_count"), data.get("miner_count"),
                data.get("alpha_price_tao"), data.get("tempo"),
                data.get("immunity_period"),
            ))


def insert_validator_snapshots(ts: int, netuid: int, validators: list, prev_stakes: dict = None):
    prev_stakes = prev_stakes or {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO validator_snapshots
                    (ts, netuid, uid, hotkey, stake_tao, stake_delta_tao,
                     dividends, vtrust, consensus, emission_tao)
                VALUES %s
            """, [
                (
                    ts, netuid, v["uid"], v.get("hotkey"),
                    v.get("stake"),
                    round(v.get("stake", 0) - prev_stakes[v["uid"]], 6) if v["uid"] in prev_stakes else None,
                    v.get("dividends"), v.get("validator_trust"),
                    v.get("consensus"), v.get("emission_tao"),
                )
                for v in validators
            ])


def insert_churn_event(ts: int, netuid: int, new_uids: set, lost_uids: set, total_uids: int):
    churn_rate = round(len(new_uids | lost_uids) / max(total_uids, 1), 4)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO churn_events
                    (ts, netuid, registered_count, deregistered_count, churn_rate, new_uids, lost_uids)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                ts, netuid,
                len(new_uids), len(lost_uids), churn_rate,
                ",".join(str(u) for u in sorted(new_uids)),
                ",".join(str(u) for u in sorted(lost_uids)),
            ))


def insert_network_snapshot(ts: int, data: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO network_snapshots (ts, block, total_issuance_tao, total_stake_tao)
                VALUES (%s,%s,%s,%s)
            """, (ts, data.get("block"), data.get("total_issuance_tao"), data.get("total_stake_tao")))


def insert_price_snapshot(ts: int, price: float, change: float, mcap: float):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO price_snapshots (ts, price_usd, change_24h, market_cap)
                VALUES (%s,%s,%s,%s)
            """, (ts, price, change, mcap))


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_subnet_history(netuid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM subnet_snapshots
            WHERE netuid = %s AND ts >= %s ORDER BY ts ASC
        """, (netuid, since))


def get_price_history(days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM price_snapshots WHERE ts >= %s ORDER BY ts ASC
        """, (since,))


def get_network_history(days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM network_snapshots WHERE ts >= %s ORDER BY ts ASC
        """, (since,))


def get_latest_subnet_snapshot(netuid: int):
    with get_conn() as conn:
        return _one(conn, """
            SELECT * FROM subnet_snapshots WHERE netuid = %s ORDER BY ts DESC LIMIT 1
        """, (netuid,))


def get_latest_validator_stakes(netuid: int) -> dict:
    with get_conn() as conn:
        rows = _rows(conn, """
            SELECT uid, stake_tao FROM validator_snapshots
            WHERE netuid = %s AND ts = (
                SELECT MAX(ts) FROM validator_snapshots WHERE netuid = %s
            )
        """, (netuid, netuid))
    return {r["uid"]: r["stake_tao"] for r in rows if r["stake_tao"] is not None}


def get_latest_uids(netuid: int) -> set | None:
    with get_conn() as conn:
        latest_ts = _one(conn, """
            SELECT MAX(ts) as max_ts FROM validator_snapshots WHERE netuid = %s
        """, (netuid,))
        if not latest_ts or latest_ts["max_ts"] is None:
            return None
        ts = latest_ts["max_ts"]
        v_rows = _rows(conn, "SELECT uid FROM validator_snapshots WHERE netuid = %s AND ts = %s", (netuid, ts))
        m_rows = _rows(conn, "SELECT uid FROM miner_snapshots WHERE netuid = %s AND ts = %s", (netuid, ts))
    uids = {r["uid"] for r in v_rows} | {r["uid"] for r in m_rows}
    return uids if uids else None


def get_churn_history(netuid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM churn_events
            WHERE netuid = %s AND ts >= %s ORDER BY ts ASC
        """, (netuid, since))


def insert_miner_snapshots(ts: int, netuid: int, miners: list):
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO miner_snapshots (ts, netuid, uid, hotkey, incentive, consensus, emission_tao)
                VALUES %s
            """, [
                (ts, netuid, m["uid"], m.get("hotkey"), m.get("incentive"), m.get("consensus"), m.get("emission_tao"))
                for m in miners
            ])


def insert_github_activity(ts: int, netuid: int, data: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO github_activity
                    (ts, netuid, repo_url, commits_7d, commits_30d, open_prs, open_issues, stars, forks)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                ts, netuid, data.get("repo_url"),
                data.get("commits_7d"), data.get("commits_30d"),
                data.get("open_prs"), data.get("open_issues"),
                data.get("stars"), data.get("forks"),
            ))


def get_miner_history(netuid: int, uid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM miner_snapshots
            WHERE netuid = %s AND uid = %s AND ts >= %s ORDER BY ts ASC
        """, (netuid, uid, since))


def get_miner_snapshot(netuid: int, ts: int) -> list[dict]:
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM miner_snapshots WHERE netuid = %s AND ts = %s
        """, (netuid, ts))


def get_github_history(netuid: int, days: int = 90) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM github_activity
            WHERE netuid = %s AND ts >= %s ORDER BY ts ASC
        """, (netuid, since))


def insert_weight_snapshots(ts: int, netuid: int, weights: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO weight_snapshots (ts, netuid, validator_uid, weights_json)
                VALUES %s
            """, [
                (ts, netuid, int(uid), json.dumps(w))
                for uid, w in weights.items()
            ])


def get_weight_snapshot(netuid: int, ts: int = None) -> list[dict]:
    with get_conn() as conn:
        if ts is None:
            row = _one(conn, "SELECT MAX(ts) AS max_ts FROM weight_snapshots WHERE netuid = %s", (netuid,))
            ts = row["max_ts"] if row else None
        if ts is None:
            return []
        rows = _rows(conn, """
            SELECT validator_uid, weights_json FROM weight_snapshots
            WHERE netuid = %s AND ts = %s
        """, (netuid, ts))
    return [{"validator_uid": r["validator_uid"], "weights": json.loads(r["weights_json"])} for r in rows]


def get_alpha_price_history(netuid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        return _rows(conn, """
            SELECT ts, alpha_price_tao FROM subnet_snapshots
            WHERE netuid = %s AND ts >= %s AND alpha_price_tao IS NOT NULL
            ORDER BY ts ASC
        """, (netuid, since))


def get_emission_coverage(netuids: list) -> dict:
    with get_conn() as conn:
        rows = _rows(conn, """
            SELECT netuid, COUNT(total_emission_tao) as emission_count
            FROM subnet_snapshots
            WHERE netuid = ANY(%s)
            GROUP BY netuid
        """, (netuids,))
    return {r["netuid"]: r["emission_count"] for r in rows}


def get_active_netuids(days: int = 7) -> list[int]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = _rows(conn, """
            SELECT DISTINCT netuid FROM subnet_snapshots
            WHERE ts >= %s ORDER BY netuid ASC
        """, (since,))
    return [r["netuid"] for r in rows]


# ── Bot memory ────────────────────────────────────────────────────────────────

def load_memory() -> dict | None:
    try:
        with get_conn() as conn:
            row = _one(conn, "SELECT data FROM bot_memory WHERE id = 1")
            return json.loads(row["data"]) if row else None
    except Exception:
        return None


def save_memory(mem: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_memory (id, data) VALUES (1, %s)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """, (json.dumps(mem),))


def get_config(key: str) -> str | None:
    try:
        with get_conn() as conn:
            row = _one(conn, "SELECT value FROM bot_config WHERE key = %s", (key,))
            return row["value"] if row else None
    except Exception:
        return None


def set_config(key: str, value: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_config (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, value))


# ── Relay (on-chain query bridge) ─────────────────────────────────────────────

def insert_relay_query(query_id: int, input_text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO relay_queries (query_id, input_text, received_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (query_id) DO NOTHING
            """, (query_id, input_text, int(time.time())))


def get_relay_input(query_id: int) -> str | None:
    with get_conn() as conn:
        row = _one(conn, "SELECT input_text FROM relay_queries WHERE query_id = %s", (query_id,))
    return row["input_text"] if row else None


def set_relay_result(query_id: int, result_text: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE relay_queries
                SET result_text = %s, fulfilled_at = %s
                WHERE query_id = %s
            """, (result_text, int(time.time()), query_id))


def get_relay_result(query_id: int) -> dict | None:
    with get_conn() as conn:
        return _one(conn, """
            SELECT input_text, result_text, received_at, fulfilled_at
            FROM relay_queries WHERE query_id = %s
        """, (query_id,))

def insert_signals(ts: int, netuid:int, score:float, confidence:float):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_history (ts, netuid, score, confidence)
                VALUES (%s, %s, %s, %s)
            """, (ts, netuid, score, confidence))
def get_recent_model_signals(netuid: int, model: str, cycles: int = 3) -> list[dict]:
    with get_conn() as conn:
        return _rows(conn, """
            SELECT ts, model, score, confidence FROM model_signal_history
            WHERE netuid = %s AND model = %s ORDER BY ts DESC LIMIT %s
        """, (netuid, model, cycles))

def get_recent_signals(netuid: int, cycles: int = 2) -> list[dict]:
    with get_conn() as conn:
        return _rows(conn, """
            SELECT ts, score, confidence FROM signal_history
            WHERE netuid = %s ORDER BY ts DESC LIMIT %s
        """, (netuid, cycles))

def get_score_percentile(pct: float, days: int) -> float | None:
    """Rolling percentile of recent cross-sectional scores — the adaptive entry bar."""
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        row = _one(conn, """
            SELECT percentile_cont(%s) WITHIN GROUP (ORDER BY score) AS p
            FROM signal_history WHERE ts >= %s
        """, (pct, since))
    return row["p"] if row and row["p"] is not None else None
def open_positions(ts: int, netuid: int, entry_price: float, size_tao: float) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO paper_positions (entry_ts, netuid, entry_price, size_tao, peak_price)
                VALUES (%s, %s, %s, %s, %s) RETURNING *
            """, (ts, netuid, entry_price, size_tao, entry_price))
def close_positions(netuid: int, exit_ts: int, exit_price: float, exit_reason: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE paper_positions
                SET status = 'closed', exit_ts = %s, exit_price = %s, exit_reason = %s,
                    pnl_tao = ROUND(CAST((%s - entry_price)/entry_price * size_tao AS NUMERIC), 6)
                WHERE netuid = %s AND status = 'open'
            """, (exit_ts, exit_price, exit_reason, exit_price, netuid))
def get_position(netuid: int) -> list[dict]:
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM paper_positions
            WHERE netuid = %s and status = 'open' ORDER BY entry_ts DESC
        """, (netuid,))
    
def get_all_positions() -> list[dict]:
    with get_conn() as conn:
        return _rows(conn, """
            SELECT * FROM paper_positions
            WHERE status = 'open' ORDER BY entry_ts DESC
        """)
def update_peak_price(netuid: int, new_price: float):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE paper_positions
                SET peak_price = GREATEST(peak_price, %s)
                WHERE netuid = %s AND status = 'open'
            """, (new_price, netuid))
        
def insert_model_signals(ts, netuid, signals):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for s in signals:
                cur.execute("""
                    INSERT INTO model_signal_history (ts, netuid, model, score, confidence)
                    VALUES (%s, %s, %s, %s, %s)
                """, (ts, netuid, s.model, s.score, s.confidence))
def get_model_signals_at_time(netuid: int, ts: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT ON (model) model, score, confidence FROM model_signal_history
                WHERE netuid = %s AND ts <= %s ORDER BY model, ts DESC
            """, (netuid, ts))
            return [{"model": r[0], "score": r[1], "confidence": r[2]} for r in cur.fetchall()]
def get_signal_weight(model: str) -> float:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT weight FROM signal_weights WHERE model = %s", (model,))
            row = cur.fetchone()
            return row[0] if row else 1.0
def update_signal_weight(model: str, weight: float):
    with get_conn() as conn:
        with conn.cursor() as curr:
            curr.execute("""
                INSERT INTO signal_weights (model, weight) VALUES (%s, %s)
                ON CONFLICT (model) DO UPDATE SET weight = EXCLUDED.weight
            """, (model, weight))

def get_all_signal_weights() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT model, weight FROM signal_weights")
            return {row[0]: row[1] for row in cur.fetchall()}

def get_last_exit_ts(netuid: int) -> int | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(exit_ts) FROM paper_positions WHERE netuid = %s AND exit_ts IS NOT NULL",
                (netuid,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def get_closed_positions(limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        return _rows(conn, """
            SELECT netuid, entry_ts, entry_price, exit_ts, exit_price, exit_reason, pnl_tao
            FROM paper_positions
            WHERE status != 'open' AND exit_price IS NOT NULL
            ORDER BY exit_ts DESC
            LIMIT %s
        """, (limit,))

# ── Subscriptions ─────────────────────────────────────────────────────────────

def upsert_subscription(wallet: str, tx_hash: str, expires_at: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscriptions (wallet, tx_hash, created_at, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (wallet) DO UPDATE
                    SET tx_hash = EXCLUDED.tx_hash,
                        created_at = EXCLUDED.created_at,
                        expires_at = EXCLUDED.expires_at
            """, (wallet.lower(), tx_hash, int(time.time()), expires_at))


def get_subscription(wallet: str) -> dict | None:
    with get_conn() as conn:
        return _one(conn, "SELECT * FROM subscriptions WHERE wallet = %s", (wallet.lower(),))


def is_subscribed(wallet: str) -> bool:
    sub = get_subscription(wallet)
    return sub is not None and sub["expires_at"] > int(time.time())


def create_access_code(wallet: str) -> str:
    code = secrets.token_urlsafe(16)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO access_codes (code, wallet, created_at)
                VALUES (%s, %s, %s)
            """, (code, wallet.lower(), int(time.time())))
    return code


def claim_access_code(code: str, telegram_id: int) -> str | None:
    with get_conn() as conn:
        row = _one(conn, "SELECT * FROM access_codes WHERE code = %s", (code,))
        if not row or row["used_at"] is not None:
            return None
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE access_codes SET telegram_id = %s, used_at = %s WHERE code = %s
            """, (telegram_id, int(time.time()), code))
        return row["wallet"]


def get_telegram_subscription(telegram_id: int) -> dict | None:
    with get_conn() as conn:
        return _one(conn, """
            SELECT s.* FROM subscriptions s
            JOIN access_codes a ON a.wallet = s.wallet
            WHERE a.telegram_id = %s AND s.expires_at > %s
        """, (telegram_id, int(time.time())))
