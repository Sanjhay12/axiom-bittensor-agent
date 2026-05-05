import asyncio
import json
import logging
import os
import re
import sys
import time
import httpx
import github as gh
import reddit as rd

logger = logging.getLogger(__name__)

_cache: dict = {}
TTL = {"detail": 120, "overview": 1800, "metagraph": 60, "network": 60, "price": 60, "social": 900}


async def fetch_tao_price() -> str:
    cached = _get("tao_price", TTL["price"])
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bittensor", "vs_currencies": "usd", "include_24hr_change": "true", "include_market_cap": "true"},
            )
            if r.status_code == 200:
                data = r.json().get("bittensor", {})
                price = data.get("usd")
                change = data.get("usd_24h_change")
                mcap = data.get("usd_market_cap")
                parts = [f"### TAO Price (Live)"]
                if price is not None:
                    parts.append(f"- Price: ${price:,.2f}")
                if change is not None:
                    parts.append(f"- 24h Change: {change:+.2f}%")
                if mcap is not None:
                    parts.append(f"- Market Cap: ${mcap:,.0f}")
                text = "\n".join(parts)
                return _set("tao_price", text)
    except Exception:
        pass
    return ""

WORKER = os.path.join(os.path.dirname(__file__), "chain_worker.py")
PYTHON = sys.executable


def _get(key, ttl):
    e = _cache.get(key)
    return e[0] if e and time.time() - e[1] < ttl else None


def _set(key, val):
    _cache[key] = (val, time.time())
    return val


class ChainReader:
    def __init__(self):
        self._proc = None
        self._ready = False
        self._startup_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()

    async def _ensure_proc(self):
        if self._ready and self._proc and self._proc.returncode is None:
            return True
        async with self._startup_lock:
            if self._ready and self._proc and self._proc.returncode is None:
                return True
            self._ready = False
            logger.info("Starting chain worker subprocess...")
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    PYTHON, WORKER,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=None,
                )
                deadline = asyncio.get_event_loop().time() + 90
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
                    line_str = line.decode().strip()
                    if not line_str:
                        continue
                    try:
                        msg = json.loads(line_str)
                        if msg.get("status") == "ready":
                            self._ready = True
                            logger.info("Chain worker ready.")
                            return True
                    except json.JSONDecodeError:
                        continue
            except asyncio.TimeoutError:
                logger.error("Chain worker timed out during startup.")
                if self._proc:
                    self._proc.kill()
                return False
            except Exception as e:
                logger.error(f"Chain worker startup failed: {e}")
                return False

    async def _call(self, cmd: dict, timeout: int = 60) -> dict:
        if not await self._ensure_proc():
            return {"error": "chain worker unavailable"}
        async with self._io_lock:
            try:
                self._proc.stdin.write((json.dumps(cmd) + "\n").encode())
                await self._proc.stdin.drain()
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
                return json.loads(line.decode().strip())
            except asyncio.TimeoutError:
                logger.error(f"Chain worker call timed out: {cmd}")
                # Kill the subprocess so the stale response doesn't poison the next read.
                self._ready = False
                if self._proc:
                    self._proc.kill()
                return {"error": "timeout"}
            except Exception as e:
                logger.error(f"Chain worker call failed: {e}")
                return {"error": str(e)}

    async def subnet_detail(self, netuid: int) -> str:
        key = f"detail_{netuid}"
        cached = _get(key, TTL["detail"])
        if cached:
            return cached

        data = await self._call({"action": "subnet_detail", "netuid": netuid})
        if "error" in data:
            logger.error(f"SN{netuid} worker error: {data['error']}")
            return f"Error fetching SN{netuid}: {data['error']}"

        lines = [f"### Subnet {netuid} — Live On-Chain Data"]
        if data.get("neurons") is not None:
            lines.append(f"- Neurons: {data['neurons']}/{data.get('max_neurons', '?')}")
        if data.get("reg_cost_tao") is not None:
            lines.append(f"- Registration Cost: {data['reg_cost_tao']:.4f} TAO")
        if data.get("tempo") is not None:
            lines.append(f"- Tempo: {data['tempo']} blocks")
        if data.get("immunity_period") is not None:
            lines.append(f"- Immunity Period: {data['immunity_period']} blocks")
        ev = data.get("total_emission_tao")
        if ev is not None and ev != 0:
            lines.append(f"- Total Emission: {ev:.4f} TAO")
        for field, label in [
            ("kappa", "Kappa"),
            ("rho", "Rho"),
            ("min_allowed_weights", "Min Allowed Weights"),
            ("max_weight_limit", "Max Weight Limit"),
            ("max_validators", "Max Validators"),
            ("weights_rate_limit", "Weights Rate Limit"),
            ("adjustment_interval", "Adjustment Interval"),
            ("bonds_moving_avg", "Bonds Moving Avg"),
            ("liquid_alpha_enabled", "Liquid Alpha"),
        ]:
            if data.get(field) is not None:
                lines.append(f"- {label}: {data[field]}")

        text = "\n".join(lines)
        return _set(key, text)

    async def metagraph_summary(self, netuid: int) -> str:
        key = f"metagraph_{netuid}"
        cached = _get(key, TTL["metagraph"])
        if cached:
            return cached

        data = await self._call({"action": "metagraph", "netuid": netuid}, timeout=120)
        if "error" in data:
            return f"Error fetching metagraph SN{netuid}: {data['error']}"

        lines = [f"### Subnet {netuid} — Metagraph (Live)"]
        lines.append(f"- Total neurons: {data.get('n', 'N/A')}")
        if data.get("validator_count") is not None:
            lines.append(f"- Active Validators: {data['validator_count']}")
        if data.get("miner_count") is not None:
            lines.append(f"- Active Miners: {data['miner_count']}")
        lines.append(f"- Total stake: {data.get('total_stake_tao', 'N/A')} TAO")
        lines.append(f"- Total emission: {data.get('total_emission_tao', data.get('total_emission', 'N/A'))}")

        if data.get("partial"):
            lines.append("- Note: rank/trust not available (removed from pallet in dTAO upgrade; use incentive/vtrust/consensus instead)")

        def _fmt(val, decimals=4):
            return f"{val:.{decimals}f}" if val is not None else "N/A"

        validators = data.get("top_validators", [])
        if validators:
            lines.append("#### Top Validators (by stake)")
            for v in validators:
                lines.append(
                    f"  - UID {v['uid']}: stake={v['stake']} TAO, "
                    f"vtrust={_fmt(v.get('validator_trust'))}, "
                    f"dividends={_fmt(v.get('dividends'))}, "
                    f"emission={_fmt(v.get('emission_tao'), 6)} TAO"
                )

        miners = data.get("top_miners", [])
        if miners:
            lines.append("#### Top Miners (by emission)")
            for m in miners:
                lines.append(
                    f"  - UID {m['uid']}: emission={_fmt(m.get('emission_tao'), 6)} TAO, "
                    f"rank={_fmt(m.get('rank'))}, trust={_fmt(m.get('trust'))}, "
                    f"incentive={_fmt(m.get('incentive'))}"
                )

        text = "\n".join(lines)
        return _set(key, text)

    async def network_info(self) -> str:
        key = "network_info"
        cached = _get(key, TTL["network"])
        if cached:
            return cached

        data = await self._call({"action": "network_info"}, timeout=30)
        if "error" in data:
            return ""

        lines = ["### Bittensor Network Info (Live)"]
        if data.get("block") is not None:
            lines.append(f"- Current Block: {data['block']}")
        if data.get("total_issuance_tao") is not None:
            lines.append(f"- Total Issuance: {data['total_issuance_tao']:.2f} TAO")
        if data.get("total_stake_tao") is not None:
            lines.append(f"- Total Staked: {data['total_stake_tao']:.2f} TAO")

        text = "\n".join(lines)
        return _set(key, text)

    async def hotkey_info(self, hotkey: str) -> str:
        data = await self._call({"action": "hotkey_info", "hotkey": hotkey}, timeout=30)
        lines = [f"### Hotkey {hotkey[:16]}... — Live Data"]
        if data.get("total_stake_tao") is not None:
            lines.append(f"- Total Stake: {data['total_stake_tao']:.4f} TAO")
        if data.get("subnets") is not None:
            lines.append(f"- Active on subnets: {data['subnets']}")
        return "\n".join(lines)

    async def subnet_overview(self) -> str:
        key = "overview"
        cached = _get(key, TTL["overview"])
        if cached:
            return cached

        data = await self._call({"action": "all_subnets"}, timeout=120)
        if "error" in data:
            return f"Error fetching subnet overview: {data['error']}"

        subnets = data.get("subnets", [])
        try:
            subnets = sorted(subnets, key=lambda s: s.get("reg_cost_tao") or 0, reverse=True)
        except Exception:
            pass

        lines = ["### All Active Subnets (live on-chain)"]
        for s in subnets:
            if "error" in s:
                lines.append(f"- SN{s['netuid']}: data unavailable")
                continue
            neurons = f"{s.get('neurons')}/{s.get('max_neurons')}" if s.get("neurons") is not None else "N/A"
            cost = f"{s['reg_cost_tao']:.4f} TAO" if s.get("reg_cost_tao") is not None else "N/A"
            ev = s.get("total_emission_tao")
            emission = f"{ev:.4f} TAO" if ev else "N/A"
            lines.append(f"- SN{s['netuid']}: neurons={neurons}, reg_cost={cost}, emission={emission}")

        text = "\n".join(lines)
        return _set(key, text)

    async def subnet_github(self, netuid: int) -> str:
        key = f"github_{netuid}"
        cached = _get(key, 3600)  # cache for 1 hour — READMEs don't change often
        if cached:
            return cached

        identity = await self._call({"action": "subnet_identity", "netuid": netuid}, timeout=15)
        if "error" in identity or not identity:
            return ""

        github_url = identity.get("github_repo") or identity.get("github") or identity.get("url") or ""
        if "github.com" not in github_url:
            return ""

        readme, meta, commits, prs, releases = await asyncio.gather(
            gh.fetch_readme(github_url),
            gh.fetch_repo_meta(github_url),
            gh.fetch_commits(github_url),
            gh.fetch_pull_requests(github_url),
            gh.fetch_releases(github_url),
        )
        if not any([readme, meta, commits, prs, releases]):
            return ""

        lines = [f"### Subnet {netuid} — GitHub ({github_url})"]
        if meta:
            parts = []
            if meta.get("stars") is not None:
                parts.append(f"stars={meta['stars']}")
            if meta.get("forks") is not None:
                parts.append(f"forks={meta['forks']}")
            if meta.get("open_issues") is not None:
                parts.append(f"open_issues={meta['open_issues']}")
            if meta.get("last_push"):
                parts.append(f"last_push={meta['last_push']}")
            if parts:
                lines.append("- " + ", ".join(parts))
            if meta.get("description"):
                lines.append(f"- {meta['description']}")

        if releases:
            lines.append("#### Recent Releases")
            for rel in releases:
                tag = f"{rel['tag']} (pre-release)" if rel["prerelease"] else rel["tag"]
                lines.append(f"  - {tag}: {rel['name']} — {rel['published']}")

        if commits:
            lines.append("#### Recent Commits")
            for c in commits:
                lines.append(f"  - [{c['sha']}] {c['date']} {c['author']}: {c['message']}")

        if prs:
            lines.append("#### Open Pull Requests")
            for p in prs:
                draft = " (draft)" if p["draft"] else ""
                lines.append(f"  - #{p['number']}{draft} {p['updated']}: {p['title']} — @{p['author']}")

        if readme:
            lines.append("#### README")
            lines.append(readme)

        text = "\n".join(lines)
        return _set(key, text)

    async def subnet_identity_summary(self, netuid: int) -> str:
        key = f"identity_{netuid}"
        cached = _get(key, 3600)
        if cached:
            return cached

        data = await self._call({"action": "subnet_identity", "netuid": netuid}, timeout=15)
        if not data or "error" in data:
            return ""

        lines = [f"### Subnet {netuid} — On-Chain Identity"]
        for field, label in [
            ("name", "Name"), ("description", "Description"),
            ("url", "Website"), ("github_repo", "GitHub"),
            ("discord", "Discord"),
        ]:
            if data.get(field):
                lines.append(f"- {label}: {data[field]}")

        text = "\n".join(lines)
        return _set(key, text)

    async def subnet_reddit(self, netuid: int) -> str:
        key = f"reddit_{netuid}"
        cached = _get(key, TTL["social"])
        if cached:
            return cached

        identity = await self._call({"action": "subnet_identity", "netuid": netuid}, timeout=15)
        name = ""
        if identity and "error" not in identity:
            name = identity.get("name", "")

        text = await rd.subnet_reddit_context(netuid, name=name)
        return _set(key, text) if text else ""

    async def bittensor_reddit(self) -> str:
        key = "reddit_bittensor"
        cached = _get(key, TTL["social"])
        if cached:
            return cached

        text = await rd.bittensor_reddit_context()
        return _set(key, text) if text else ""

    async def prewarm(self):
        logger.info("Prewarm: starting chain worker...")
        await self._ensure_proc()


def _extract_netuid(text: str):
    m = re.search(r'\b(?:sub\w{0,4}|sn|netuid)\s*#?(\d+)\b', text.lower())
    return int(m.group(1)) if m else None


async def gather_chain_context(query: str, reader, recent_history: list = None, plan: dict = None) -> str:
    if reader is None:
        return ""

    q = query.lower()
    hotkey_match = re.search(r'\b5[A-Za-z0-9]{47}\b', query)
    parts = []

    if plan is not None:
        # Use Haiku's fetch plan
        netuid = plan.get("netuid")
        fetch = set(plan.get("fetch", []))

        if netuid is not None:
            if fetch:
                parts.append(await reader.subnet_detail(netuid))
            subnet_fetches = []
            if "metagraph" in fetch:
                subnet_fetches.append(reader.metagraph_summary(netuid))
            if "github" in fetch:
                subnet_fetches.append(reader.subnet_github(netuid))
            if "identity" in fetch:
                subnet_fetches.append(reader.subnet_identity_summary(netuid))
            if "reddit" in fetch:
                subnet_fetches.append(reader.subnet_reddit(netuid))
            if subnet_fetches:
                parts.extend(await asyncio.gather(*subnet_fetches))

        global_fetches = []
        if "network" in fetch:
            global_fetches.append(reader.network_info())
        if "overview" in fetch:
            global_fetches.append(reader.subnet_overview())
        if "price" in fetch:
            global_fetches.append(fetch_tao_price())
        if "reddit" in fetch and netuid is None:
            global_fetches.append(reader.bittensor_reddit())
        if global_fetches:
            parts.extend(await asyncio.gather(*global_fetches))

        if "hotkey" in fetch and hotkey_match:
            parts.append(await reader.hotkey_info(hotkey_match.group(0)))

    else:
        # Keyword fallback
        netuid = _extract_netuid(query)
        if netuid is None and recent_history:
            for msg in reversed(recent_history):
                found = _extract_netuid(msg.get("content", ""))
                if found is not None:
                    netuid = found
                    break

        if netuid is not None:
            parts.append(await reader.subnet_detail(netuid))

            metagraph_keywords = ["validator", "miner", "stake", "rank", "trust", "emission", "dividend", "incentive", "vtrust", "consensus", "uid", "who", "top", "best", "neuron", "metagraph", "pull", "fetch", "show", "get", "summary", "data", "breakdown", "more", "detail"]
            if any(w in q for w in metagraph_keywords):
                parts.append(await reader.metagraph_summary(netuid))

            github_keywords = ["github", "repo", "code", "readme", "what is", "what does", "about", "memo", "research", "explain"]
            if any(w in q for w in github_keywords):
                parts.append(await reader.subnet_github(netuid))

        network_keywords = ["total supply", "issuance", "total stake", "network", "current block", "how much tao"]
        if any(w in q for w in network_keywords):
            parts.append(await reader.network_info())

        overview_keywords = ["overview", "list all", "top subnets", "best subnet", "all subnets", "compare subnet", "which subnet"]
        if any(w in q for w in overview_keywords):
            parts.append(await reader.subnet_overview())

    if hotkey_match:
        parts.append(await reader.hotkey_info(hotkey_match.group(0)))

    return "\n\n".join(p for p in parts if p)
