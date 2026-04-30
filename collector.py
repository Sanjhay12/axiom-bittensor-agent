"""
Background data collection job.
Runs every INTERVAL seconds and snapshots key Bittensor metrics into SQLite.
"""
import asyncio
import logging
import time
import httpx

import store

logger = logging.getLogger(__name__)

INTERVAL = 4 * 3600  # every 4 hours


async def _fetch_price() -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "bittensor",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_market_cap": "true",
                },
            )
            if r.status_code == 200:
                d = r.json().get("bittensor", {})
                return {
                    "price": d.get("usd"),
                    "change": d.get("usd_24h_change"),
                    "mcap": d.get("usd_market_cap"),
                }
    except Exception:
        pass
    return {}


async def collect_once(reader):
    ts = int(time.time())
    logger.info(f"Collector: starting snapshot at {ts}")

    # ── All subnets overview ──────────────────────────────────────────────────
    try:
        overview = await reader._call({"action": "all_subnets"}, timeout=120)
        for s in overview.get("subnets", []):
            if "error" in s:
                continue
            store.insert_subnet_snapshot(ts, s["netuid"], {
                "neuron_count":  s.get("neurons"),
                "max_neurons":   s.get("max_neurons"),
                "reg_cost_tao":  s.get("reg_cost_tao"),
                "emission_value": s.get("emission_value"),
            })
        logger.info(f"Collector: stored {len(overview.get('subnets', []))} subnet snapshots")
    except Exception as e:
        logger.error(f"Collector: subnet overview failed: {e}")

    # ── Watched subnets — full metagraph ─────────────────────────────────────
    watched = _get_watched_netuids()
    for netuid in watched:
        try:
            meta = await reader._call({"action": "metagraph", "netuid": netuid}, timeout=120)
            if "error" in meta:
                continue
            validators = meta.get("top_validators", [])
            miners = meta.get("top_miners", [])
            store.insert_subnet_snapshot(ts, netuid, {
                "total_stake_tao":    meta.get("total_stake_tao"),
                "total_emission_tao": meta.get("total_emission_tao"),
                "validator_count":    len(validators),
                "miner_count":        len(miners),
                "neuron_count":       meta.get("n"),
            })
            if validators:
                store.insert_validator_snapshots(ts, netuid, validators)
            logger.info(f"Collector: stored metagraph for SN{netuid}")
        except Exception as e:
            logger.error(f"Collector: metagraph SN{netuid} failed: {e}")

    # ── Network info ─────────────────────────────────────────────────────────
    try:
        net = await reader._call({"action": "network_info"}, timeout=30)
        if "error" not in net:
            store.insert_network_snapshot(ts, net)
            logger.info("Collector: stored network snapshot")
    except Exception as e:
        logger.error(f"Collector: network info failed: {e}")

    # ── TAO price ────────────────────────────────────────────────────────────
    try:
        price = await _fetch_price()
        if price.get("price") is not None:
            store.insert_price_snapshot(
                ts, price["price"], price.get("change", 0), price.get("mcap", 0)
            )
            logger.info(f"Collector: stored price ${price['price']:.2f}")
    except Exception as e:
        logger.error(f"Collector: price fetch failed: {e}")

    logger.info("Collector: snapshot complete")


def _get_watched_netuids() -> list:
    """Load watched subnets from memory.json."""
    try:
        import memory as mem
        data = mem.load()
        return data.get("watched_subnets", [])
    except Exception:
        return []


async def run_loop(reader):
    """Run collection on startup then every INTERVAL seconds."""
    store.init_db()
    while True:
        try:
            await collect_once(reader)
        except Exception as e:
            logger.error(f"Collector loop error: {e}")
        await asyncio.sleep(INTERVAL)
