import json
import sqlite3
import os
import time

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(_DATA_DIR, "bittensor_history.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subnet_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                netuid          INTEGER NOT NULL,
                neuron_count    INTEGER,
                max_neurons     INTEGER,
                reg_cost_tao    REAL,
                emission_value  REAL,
                total_stake_tao REAL,
                total_emission_tao REAL,
                validator_count INTEGER,
                miner_count     INTEGER,
                alpha_price_tao REAL,
                tempo           INTEGER,
                immunity_period INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_subnet_ts
                ON subnet_snapshots(netuid, ts);

            CREATE TABLE IF NOT EXISTS validator_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
            );
            CREATE INDEX IF NOT EXISTS idx_validator_ts
                ON validator_snapshots(netuid, ts);

            CREATE TABLE IF NOT EXISTS churn_events (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  INTEGER NOT NULL,
                netuid              INTEGER NOT NULL,
                registered_count    INTEGER,
                deregistered_count  INTEGER,
                churn_rate          REAL,
                new_uids            TEXT,
                lost_uids           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_churn_ts
                ON churn_events(netuid, ts);

            CREATE TABLE IF NOT EXISTS network_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  INTEGER NOT NULL,
                block               INTEGER,
                total_issuance_tao  REAL,
                total_stake_tao     REAL
            );

            CREATE TABLE IF NOT EXISTS price_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                price_usd   REAL,
                change_24h  REAL,
                market_cap  REAL
            );

            CREATE TABLE IF NOT EXISTS miner_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           INTEGER NOT NULL,
                netuid       INTEGER NOT NULL,
                uid          INTEGER NOT NULL,
                hotkey       TEXT,
                incentive    REAL,
                consensus    REAL,
                emission_tao REAL
            );
            CREATE INDEX IF NOT EXISTS idx_miner_ts
                ON miner_snapshots(netuid, ts);

            CREATE TABLE IF NOT EXISTS github_activity (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            INTEGER NOT NULL,
                netuid        INTEGER NOT NULL,
                repo_url      TEXT,
                commits_7d    INTEGER,
                commits_30d   INTEGER,
                open_prs      INTEGER,
                open_issues   INTEGER,
                stars         INTEGER,
                forks         INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_github_ts
                ON github_activity(netuid, ts);

            CREATE TABLE IF NOT EXISTS weight_snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                netuid          INTEGER NOT NULL,
                validator_uid   INTEGER NOT NULL,
                weights_json    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_weight_ts
                ON weight_snapshots(netuid, ts);
        """)
        # Migrate existing DBs that lack new columns
        for sql in [
            "ALTER TABLE subnet_snapshots ADD COLUMN alpha_price_tao REAL",
            "ALTER TABLE subnet_snapshots ADD COLUMN tempo INTEGER",
            "ALTER TABLE subnet_snapshots ADD COLUMN immunity_period INTEGER",
            "ALTER TABLE validator_snapshots ADD COLUMN stake_delta_tao REAL",
            "ALTER TABLE validator_snapshots ADD COLUMN consensus REAL",
        ]:
            try:
                conn.execute(sql)
            except Exception:
                pass


def insert_subnet_snapshot(ts: int, netuid: int, data: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO subnet_snapshots
                (ts, netuid, neuron_count, max_neurons, reg_cost_tao,
                 emission_value, total_stake_tao, total_emission_tao,
                 validator_count, miner_count, alpha_price_tao,
                 tempo, immunity_period)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ts, netuid,
            data.get("neuron_count"),
            data.get("max_neurons"),
            data.get("reg_cost_tao"),
            data.get("emission_value"),
            data.get("total_stake_tao"),
            data.get("total_emission_tao"),
            data.get("validator_count"),
            data.get("miner_count"),
            data.get("alpha_price_tao"),
            data.get("tempo"),
            data.get("immunity_period"),
        ))


def insert_validator_snapshots(ts: int, netuid: int, validators: list, prev_stakes: dict = None):
    prev_stakes = prev_stakes or {}
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO validator_snapshots
                (ts, netuid, uid, hotkey, stake_tao, stake_delta_tao,
                 dividends, vtrust, consensus, emission_tao)
            VALUES (?,?,?,?,?,?,?,?,?,?)
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
        conn.execute("""
            INSERT INTO churn_events
                (ts, netuid, registered_count, deregistered_count, churn_rate, new_uids, lost_uids)
            VALUES (?,?,?,?,?,?,?)
        """, (
            ts, netuid,
            len(new_uids), len(lost_uids), churn_rate,
            ",".join(str(u) for u in sorted(new_uids)),
            ",".join(str(u) for u in sorted(lost_uids)),
        ))


def insert_network_snapshot(ts: int, data: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO network_snapshots (ts, block, total_issuance_tao, total_stake_tao)
            VALUES (?,?,?,?)
        """, (ts, data.get("block"), data.get("total_issuance_tao"), data.get("total_stake_tao")))


def insert_price_snapshot(ts: int, price: float, change: float, mcap: float):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO price_snapshots (ts, price_usd, change_24h, market_cap)
            VALUES (?,?,?,?)
        """, (ts, price, change, mcap))


# ── Query helpers (used by Layer 2) ──────────────────────────────────────────

def get_subnet_history(netuid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM subnet_snapshots
            WHERE netuid = ? AND ts >= ?
            ORDER BY ts ASC
        """, (netuid, since)).fetchall()
    return [dict(r) for r in rows]


def get_price_history(days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM price_snapshots
            WHERE ts >= ? ORDER BY ts ASC
        """, (since,)).fetchall()
    return [dict(r) for r in rows]


def get_network_history(days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM network_snapshots
            WHERE ts >= ? ORDER BY ts ASC
        """, (since,)).fetchall()
    return [dict(r) for r in rows]


def get_latest_subnet_snapshot(netuid: int):
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM subnet_snapshots
            WHERE netuid = ? ORDER BY ts DESC LIMIT 1
        """, (netuid,)).fetchone()
    return dict(row) if row else None


def get_latest_validator_stakes(netuid: int) -> dict:
    """Returns {uid: stake_tao} from the most recent snapshot for this subnet."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT uid, stake_tao FROM validator_snapshots
            WHERE netuid = ? AND ts = (
                SELECT MAX(ts) FROM validator_snapshots WHERE netuid = ?
            )
        """, (netuid, netuid)).fetchall()
    return {r["uid"]: r["stake_tao"] for r in rows if r["stake_tao"] is not None}


def get_latest_uids(netuid: int) -> set | None:
    """Returns set of UIDs from the most recent validator snapshot, or None if no history."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT uid FROM validator_snapshots
            WHERE netuid = ? AND ts = (
                SELECT MAX(ts) FROM validator_snapshots WHERE netuid = ?
            )
        """, (netuid, netuid)).fetchall()
    if not rows:
        return None
    return {r["uid"] for r in rows}


def get_churn_history(netuid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM churn_events
            WHERE netuid = ? AND ts >= ? ORDER BY ts ASC
        """, (netuid, since)).fetchall()
    return [dict(r) for r in rows]


def insert_miner_snapshots(ts: int, netuid: int, miners: list):
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO miner_snapshots (ts, netuid, uid, hotkey, incentive, consensus, emission_tao)
            VALUES (?,?,?,?,?,?,?)
        """, [
            (ts, netuid, m["uid"], m.get("hotkey"), m.get("incentive"),
             m.get("consensus"), m.get("emission_tao"))
            for m in miners
        ])


def insert_github_activity(ts: int, netuid: int, data: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO github_activity
                (ts, netuid, repo_url, commits_7d, commits_30d, open_prs, open_issues, stars, forks)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            ts, netuid,
            data.get("repo_url"),
            data.get("commits_7d"),
            data.get("commits_30d"),
            data.get("open_prs"),
            data.get("open_issues"),
            data.get("stars"),
            data.get("forks"),
        ))


def get_miner_history(netuid: int, uid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM miner_snapshots
            WHERE netuid = ? AND uid = ? AND ts >= ? ORDER BY ts ASC
        """, (netuid, uid, since)).fetchall()
    return [dict(r) for r in rows]


def get_miner_snapshot(netuid: int, ts: int) -> list[dict]:
    """All miners for a subnet at a given timestamp."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM miner_snapshots WHERE netuid = ? AND ts = ?
        """, (netuid, ts)).fetchall()
    return [dict(r) for r in rows]


def get_github_history(netuid: int, days: int = 90) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM github_activity
            WHERE netuid = ? AND ts >= ? ORDER BY ts ASC
        """, (netuid, since)).fetchall()
    return [dict(r) for r in rows]


def insert_weight_snapshots(ts: int, netuid: int, weights: dict):
    """weights: {validator_uid: [[miner_uid, weight_normalized], ...]}"""
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO weight_snapshots (ts, netuid, validator_uid, weights_json)
            VALUES (?,?,?,?)
        """, [
            (ts, netuid, int(uid), json.dumps(w))
            for uid, w in weights.items()
        ])


def get_weight_snapshot(netuid: int, ts: int = None) -> list[dict]:
    """Returns all validator weight rows for a subnet at the latest (or given) timestamp."""
    with get_conn() as conn:
        if ts is None:
            ts = conn.execute(
                "SELECT MAX(ts) FROM weight_snapshots WHERE netuid = ?", (netuid,)
            ).fetchone()[0]
        if ts is None:
            return []
        rows = conn.execute("""
            SELECT validator_uid, weights_json FROM weight_snapshots
            WHERE netuid = ? AND ts = ?
        """, (netuid, ts)).fetchall()
    return [{"validator_uid": r["validator_uid"], "weights": json.loads(r["weights_json"])} for r in rows]


def get_alpha_price_history(netuid: int, days: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT ts, alpha_price_tao FROM subnet_snapshots
            WHERE netuid = ? AND ts >= ? AND alpha_price_tao IS NOT NULL
            ORDER BY ts ASC
        """, (netuid, since)).fetchall()
    return [dict(r) for r in rows]
