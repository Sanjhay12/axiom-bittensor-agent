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
                miner_count     INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_subnet_ts
                ON subnet_snapshots(netuid, ts);

            CREATE TABLE IF NOT EXISTS validator_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                netuid      INTEGER NOT NULL,
                uid         INTEGER NOT NULL,
                hotkey      TEXT,
                stake_tao   REAL,
                dividends   REAL,
                vtrust      REAL,
                emission_tao REAL
            );
            CREATE INDEX IF NOT EXISTS idx_validator_ts
                ON validator_snapshots(netuid, ts);

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
        """)


def insert_subnet_snapshot(ts: int, netuid: int, data: dict):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO subnet_snapshots
                (ts, netuid, neuron_count, max_neurons, reg_cost_tao,
                 emission_value, total_stake_tao, total_emission_tao,
                 validator_count, miner_count)
            VALUES (?,?,?,?,?,?,?,?,?,?)
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
        ))


def insert_validator_snapshots(ts: int, netuid: int, validators: list):
    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO validator_snapshots
                (ts, netuid, uid, hotkey, stake_tao, dividends, vtrust, emission_tao)
            VALUES (?,?,?,?,?,?,?,?)
        """, [
            (ts, netuid, v["uid"], v.get("hotkey"), v.get("stake"),
             v.get("dividends"), v.get("validator_trust"), v.get("emission_tao"))
            for v in validators
        ])


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
