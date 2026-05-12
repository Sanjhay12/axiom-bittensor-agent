"""
Background data collection job.
Runs every INTERVAL seconds and snapshots key Bittensor metrics into SQLite.
Full metagraph (validators + all miners) collected for every active subnet.
GitHub activity collected every GITHUB_INTERVAL seconds (once per day).
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
import httpx

import store

logger = logging.getLogger(__name__)

INTERVAL        = 4 * 3600   # metagraph + chain data every 4 hours
GITHUB_INTERVAL = 24 * 3600  # GitHub every 24 hours

_last_github_run = 0


def _github_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_github_activity(repo_url: str) -> dict:
    try:
        parts = repo_url.rstrip("/").split("/")
        if "github.com" not in repo_url or len(parts) < 5:
            return {}
        owner, repo = parts[-2], parts[-1]
        base = f"https://api.github.com/repos/{owner}/{repo}"

        since_7d  = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        async with httpx.AsyncClient(timeout=15, headers=_github_headers()) as client:
            r = await client.get(base)
            if r.status_code == 404:
                return {}
            if r.status_code != 200:
                logger.warning(f"GitHub {repo_url}: status {r.status_code}")
                return {}
            meta = r.json()

            c7  = await client.get(f"{base}/commits", params={"since": since_7d,  "per_page": 100})
            c30 = await client.get(f"{base}/commits", params={"since": since_30d, "per_page": 100})
            prs = await client.get(f"{base}/pulls",   params={"state": "open",    "per_page": 100})
            iss = await client.get(f"{base}/issues",  params={"state": "open",    "per_page": 100})

            return {
                "repo_url":    repo_url,
                "commits_7d":  len(c7.json())  if c7.status_code  == 200 else None,
                "commits_30d": len(c30.json()) if c30.status_code == 200 else None,
                "open_prs":    len(prs.json()) if prs.status_code == 200 else None,
                "open_issues": len(iss.json()) if iss.status_code == 200 else None,
                "stars":       meta.get("stargazers_count"),
                "forks":       meta.get("forks_count"),
            }
    except Exception as e:
        logger.warning(f"GitHub fetch failed for {repo_url}: {e}")
        return {}


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
    global _last_github_run
    ts = int(time.time())
    logger.info(f"Collector: starting snapshot at {ts}")

    # ── All subnets overview — gets alpha price + basic stats for every subnet ─
    all_netuids = []
    subnet_data = {}  # accumulate all fields before inserting
    try:
        overview = await reader._call({"action": "all_subnets"}, timeout=120)
        if "error" in overview or not overview.get("subnets"):
            logger.warning(f"Collector: overview returned empty/error ({overview.get('error', 'no subnets')}), restarting chain worker and retrying...")
            await reader.restart()
            await asyncio.sleep(5)
            overview = await reader._call({"action": "all_subnets"}, timeout=120)
        for s in overview.get("subnets", []):
            if "error" in s:
                continue
            netuid = s["netuid"]
            all_netuids.append(netuid)
            subnet_data[netuid] = {
                "neuron_count":    s.get("neurons"),
                "max_neurons":     s.get("max_neurons"),
                "reg_cost_tao":    s.get("reg_cost_tao"),
                "alpha_price_tao": s.get("alpha_price_tao"),
                "tempo":           s.get("tempo"),
                "immunity_period": s.get("immunity_period"),
            }
        logger.info(f"Collector: fetched overview for {len(all_netuids)} subnets")
    except Exception as e:
        logger.error(f"Collector: subnet overview failed: {e}")

    # ── Full metagraph for every active subnet ────────────────────────────────
    coverage = store.get_emission_coverage(all_netuids)
    prioritised = sorted(all_netuids, key=lambda n: coverage.get(n, 0))
    deadline = time.time() + 3 * 3600
    for netuid in prioritised:
        if time.time() > deadline:
            logger.warning(f"Collector: metagraph time budget exceeded, skipping remaining {len(all_netuids)} subnets")
            break
        try:
            meta = await reader._call({"action": "metagraph", "netuid": netuid}, timeout=120)
            if "error" in meta:
                continue

            validators      = meta.get("top_validators", [])
            all_miners_data = meta.get("all_miners", meta.get("top_miners", []))

            # Merge metagraph fields into the existing overview data
            subnet_data.setdefault(netuid, {}).update({
                "total_stake_tao":    meta.get("total_stake_tao"),
                "total_emission_tao": meta.get("total_emission_tao"),
                "validator_count":    meta.get("validator_count") or len(validators),
                "miner_count":        meta.get("miner_count") or len(all_miners_data),
                "neuron_count":       meta.get("n") or subnet_data.get(netuid, {}).get("neuron_count"),
            })

            # Insert the fully merged snapshot once
            store.insert_subnet_snapshot(ts, netuid, subnet_data[netuid])

            # Get prev UIDs before inserting current snapshots
            prev_uids = store.get_latest_uids(netuid)

            if validators:
                prev_stakes = store.get_latest_validator_stakes(netuid)
                store.insert_validator_snapshots(ts, netuid, validators, prev_stakes)

            if all_miners_data:
                store.insert_miner_snapshots(ts, netuid, all_miners_data)

            # Weights
            if validators:
                try:
                    w_data = await reader._call({"action": "weights", "netuid": netuid}, timeout=60)
                    if "error" not in w_data and w_data.get("weights"):
                        store.insert_weight_snapshots(ts, netuid, w_data["weights"])
                except Exception as e:
                    logger.warning(f"Collector: weights SN{netuid} failed: {e}")

            # Churn
            all_uids = {v["uid"] for v in validators} | {m["uid"] for m in all_miners_data}
            if prev_uids is not None and len(all_uids) > 0:
                new_uids  = all_uids - prev_uids
                lost_uids = prev_uids - all_uids
                if new_uids or lost_uids:
                    store.insert_churn_event(ts, netuid, new_uids, lost_uids, len(all_uids))
                    logger.info(f"Collector: SN{netuid} churn +{len(new_uids)} -{len(lost_uids)}")

        except Exception as e:
            logger.error(f"Collector: metagraph SN{netuid} failed: {e}")
            # Still insert whatever overview data we have for this subnet
            if netuid in subnet_data and "total_stake_tao" not in subnet_data[netuid]:
                store.insert_subnet_snapshot(ts, netuid, subnet_data[netuid])

    logger.info(f"Collector: metagraph complete for {len(all_netuids)} subnets")

    # ── GitHub — once per day for all subnets with a repo ────────────────────
    if ts - _last_github_run >= GITHUB_INTERVAL:
        logger.info("Collector: starting GitHub collection run")
        gh_count = 0
        for netuid in all_netuids:
            try:
                identity = await reader._call({"action": "subnet_identity", "netuid": netuid}, timeout=30)
                repo_url = identity.get("github_repo") or identity.get("github") or identity.get("repository")
                if not repo_url or "github.com" not in repo_url:
                    continue
                gh = await _fetch_github_activity(repo_url)
                if gh:
                    store.insert_github_activity(ts, netuid, gh)
                    gh_count += 1
                await asyncio.sleep(0.5)  # gentle rate limiting
            except Exception as e:
                logger.error(f"Collector: GitHub SN{netuid} failed: {e}")
        _last_github_run = ts
        logger.info(f"Collector: GitHub done — {gh_count} repos stored")

    # ── Network info ──────────────────────────────────────────────────────────
    try:
        net = await reader._call({"action": "network_info"}, timeout=90)
        if "error" not in net and any(net.get(k) is not None for k in ["block", "total_issuance_tao", "total_stake_tao"]):
            store.insert_network_snapshot(ts, net)
    except Exception as e:
        logger.error(f"Collector: network info failed: {e}")

    # ── TAO price ─────────────────────────────────────────────────────────────
    try:
        price = await _fetch_price()
        if price.get("price") is not None:
            store.insert_price_snapshot(
                ts, price["price"], price.get("change", 0), price.get("mcap", 0)
            )
            logger.info(f"Collector: TAO ${price['price']:.2f}")
    except Exception as e:
        logger.error(f"Collector: price fetch failed: {e}")

    logger.info("Collector: snapshot complete")


async def run_loop(reader):
    store.init_db()
    while True:
        try:
            await collect_once(reader)
        except Exception as e:
            logger.error(f"Collector loop error: {e}")
        await asyncio.sleep(INTERVAL)
