"""
One-time script to pull 90 days of historical subnet data from Taostats
and load it into a backtest_snapshots table in the DB.

Run once: python fetch_historical.py
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
import psycopg2
import psycopg2.extras
import os
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

API_KEY  = "tao-5051dd43-6fef-4879-93e6-572202de2811:bddb9604"
BASE_URL = "https://api.taostats.io"
HEADERS  = {"Authorization": API_KEY, "Accept": "application/json"}

# Pull last 90 days — adjust if you want more
DAYS_BACK   = 365
RATE_LIMIT  = 12  # seconds between calls (5/min = every 12s)


def init_backtest_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backtest_snapshots (
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_backtest_ts ON backtest_snapshots(netuid, ts)")
    conn.commit()
    logger.info("backtest_snapshots table ready")


def get_active_netuids(conn) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT netuid FROM subnet_snapshots ORDER BY netuid")
        return [r[0] for r in cur.fetchall()]


async def fetch_pool_history(client: httpx.AsyncClient, netuid: int) -> list[dict]:
    """Returns list of {ts, alpha_price_tao, total_stake_tao} sorted oldest first."""
    since_ts = int(time.time()) - DAYS_BACK * 86400
    rows = []
    page = 1

    while True:
        await asyncio.sleep(RATE_LIMIT)
        r = await client.get(f"{BASE_URL}/api/dtao/pool/history/v1",
            params={"netuid": netuid, "limit": 100, "page": page})

        if r.status_code == 429:
            logger.warning(f"Rate limited on pool/history SN{netuid}, waiting 30s")
            await asyncio.sleep(30)
            continue
        if r.status_code != 200:
            logger.warning(f"pool/history SN{netuid} status {r.status_code}")
            break

        data = r.json().get("data", [])
        if not data:
            break

        for row in data:
            row_ts = int(datetime.fromisoformat(
                row["timestamp"].replace("Z", "+00:00")).timestamp())
            if row_ts < since_ts:
                return sorted(rows, key=lambda x: x["ts"])
            rows.append({
                "ts":              row_ts,
                "alpha_price_tao": float(row["price"]),
                "total_stake_tao": int(row["total_tao"]) / 1e9,
            })

        pagination = r.json().get("pagination", {})
        if page >= pagination.get("total_pages", 1):
            break
        page += 1

    return sorted(rows, key=lambda x: x["ts"])


async def fetch_subnet_history(client: httpx.AsyncClient, netuid: int) -> list[dict]:
    """Returns list of {ts, reg_cost_tao, neuron_count, max_neurons, validator_count,
    miner_count, tempo, immunity_period} sorted oldest first."""
    since_ts = int(time.time()) - DAYS_BACK * 86400
    rows = []
    page = 1

    while True:
        await asyncio.sleep(RATE_LIMIT)
        r = await client.get(f"{BASE_URL}/api/subnet/history/v1",
            params={"netuid": netuid, "limit": 100, "page": page})

        if r.status_code == 429:
            logger.warning(f"Rate limited on subnet/history SN{netuid}, waiting 30s")
            await asyncio.sleep(30)
            continue
        if r.status_code != 200:
            logger.warning(f"subnet/history SN{netuid} status {r.status_code}")
            break

        data = r.json().get("data", [])
        if not data:
            break

        for row in data:
            row_ts = int(datetime.fromisoformat(
                row["timestamp"].replace("Z", "+00:00")).timestamp())
            if row_ts < since_ts:
                return sorted(rows, key=lambda x: x["ts"])
            reg_cost_raw = row.get("neuron_registration_cost") or 0
            rows.append({
                "ts":              row_ts,
                "reg_cost_tao":    int(reg_cost_raw) / 1e9,
                "neuron_count":    row.get("active_keys"),
                "max_neurons":     row.get("max_neurons"),
                "validator_count": row.get("validators"),
                "miner_count":     row.get("active_miners"),
                "tempo":           row.get("tempo"),
                "immunity_period": row.get("immunity_period"),
            })

        pagination = r.json().get("pagination", {})
        if page >= pagination.get("total_pages", 1):
            break
        page += 1

    return sorted(rows, key=lambda x: x["ts"])


def merge_and_compute_emission(pool_rows: list, subnet_rows: list) -> list:
    """
    Merge pool and subnet rows by nearest timestamp.
    Compute total_emission_tao as day-over-day change in total_stake_tao
    (proxy for TAO injected by network into pool).
    """
    if not pool_rows:
        return []

    # Build subnet lookup by ts
    subnet_by_ts = {r["ts"]: r for r in subnet_rows}

    merged = []
    for i, p in enumerate(pool_rows):
        # Find nearest subnet row
        nearest = min(subnet_rows, key=lambda r: abs(r["ts"] - p["ts"])) if subnet_rows else {}

        # Emission proxy: change in total_stake_tao from previous day
        if i > 0:
            emission = p["total_stake_tao"] - pool_rows[i-1]["total_stake_tao"]
            emission = max(emission, 0)  # only count inflows
        else:
            emission = None

        merged.append({
            "ts":                 p["ts"],
            "alpha_price_tao":    p["alpha_price_tao"],
            "total_stake_tao":    p["total_stake_tao"],
            "total_emission_tao": emission,
            "reg_cost_tao":       nearest.get("reg_cost_tao"),
            "neuron_count":       nearest.get("neuron_count"),
            "max_neurons":        nearest.get("max_neurons"),
            "validator_count":    nearest.get("validator_count"),
            "miner_count":        nearest.get("miner_count"),
            "tempo":              nearest.get("tempo"),
            "immunity_period":    nearest.get("immunity_period"),
        })

    return merged


def insert_backtest_rows(conn, netuid: int, rows: list):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO backtest_snapshots
                (ts, netuid, neuron_count, max_neurons, reg_cost_tao,
                 total_stake_tao, total_emission_tao,
                 validator_count, miner_count, alpha_price_tao,
                 tempo, immunity_period)
            VALUES %s
            ON CONFLICT DO NOTHING
        """, [
            (
                r["ts"], netuid,
                r.get("neuron_count"), r.get("max_neurons"),
                r.get("reg_cost_tao"),
                r.get("total_stake_tao"), r.get("total_emission_tao"),
                r.get("validator_count"), r.get("miner_count"),
                r.get("alpha_price_tao"),
                r.get("tempo"), r.get("immunity_period"),
            )
            for r in rows
        ])
    conn.commit()


async def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # fallback to Railway public URL for local run
        db_url = "postgresql://postgres:hIrjDWVQIQDuxduRhOXsoiYpcFTHlSke@switchyard.proxy.rlwy.net:32597/railway"

    conn = psycopg2.connect(db_url)
    init_backtest_table(conn)
    netuids = get_active_netuids(conn)
    conn.close()

    logger.info(f"Fetching historical data for {len(netuids)} subnets over {DAYS_BACK} days")
    logger.info(f"Estimated time: ~{len(netuids) * 2 * RATE_LIMIT / 60:.0f} minutes")

    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        for netuid in tqdm(netuids, desc="Fetching subnets", unit="subnet"):
            try:
                pool_rows   = await fetch_pool_history(client, netuid)
                subnet_rows = await fetch_subnet_history(client, netuid)
                merged      = merge_and_compute_emission(pool_rows, subnet_rows)
                conn = psycopg2.connect(db_url)
                insert_backtest_rows(conn, netuid, merged)
                conn.close()
                logger.info(f"  SN{netuid}: {len(merged)} rows inserted")
            except Exception as e:
                logger.error(f"  SN{netuid} failed: {e}")

    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
