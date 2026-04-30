import asyncio
import json
import re
import time
from chain import ChainReader, fetch_tao_price
import pdf_gen

_cache: dict = {}
MEMO_TTL = 3 * 3600  # 3 hours

MEMO_PROMPT = """\
You are writing a professional Bittensor subnet research memo. Using only the live on-chain data and GitHub information provided below, write a structured memo covering each section. Be direct and analytical. Have opinions. Flag anything that looks off.

This is for Telegram so use NO markdown, NO headers with #, NO bold with **, NO asterisks. Use plain text with section labels followed by a colon, and line breaks between sections.

Sections to cover:

OVERVIEW
What this subnet does, its purpose, the problem it solves. Pull from the GitHub README and on-chain identity. One paragraph.

NETWORK HEALTH
Neuron count vs max, registration cost, tempo, immunity period. Is the subnet growing, full, or stagnant?

VALIDATOR LANDSCAPE
Number of validators, stake concentration (who dominates), vtrust distribution. Is consensus healthy? Any red flags?

MINER LANDSCAPE
Top miners by incentive score, how competitive is it, is there meaningful differentiation or are scores clustered? Remember miner TAO emission is zero by design in dTAO — focus on incentive and consensus scores.

EMISSION & ECONOMICS
Total subnet emission, how it flows to validators as dividends. TAO price context if available. Worth validating here right now?

DEVELOPMENT ACTIVITY
Recent GitHub commits, open PRs, latest release. Is development active or stale?

RISK FACTORS
Anything suspicious or worth flagging — extreme stake concentration, low neuron count, stale development, high reg cost vs emission, anything else that stands out.

VERDICT
One honest paragraph. Your actual take on this subnet right now.

---
Live Data:
{context}
"""


async def generate(netuid: int, reader: ChainReader, claude) -> str:
    cache_key = f"memo_{netuid}"
    entry = _cache.get(cache_key)
    if entry and time.time() - entry[1] < MEMO_TTL:
        return entry[0]

    detail, metagraph, github, identity, price = await asyncio.gather(
        reader.subnet_detail(netuid),
        reader.metagraph_summary(netuid),
        reader.subnet_github(netuid),
        reader.subnet_identity_summary(netuid),
        fetch_tao_price(),
    )

    context = "\n\n".join(p for p in [detail, metagraph, github, identity, price] if p)
    if not context:
        return f"No data available for SN{netuid} right now."

    result = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": MEMO_PROMPT.format(context=context)}],
    )

    memo = result.content[0].text.strip()
    _cache[cache_key] = (memo, time.time())
    return memo


WATCHLIST_PROMPT = """\
You are a Bittensor research analyst. Given the current state of all active subnets, pick exactly 5 that are most interesting to watch right now. Consider a mix of: high emission, unusual registration costs, active development signals, notable validator concentration, or anything that stands out as worth investigating.

Also identify which single subnet among the 5 deserves the deepest investigation right now — the one with the most to uncover, the most unusual dynamics, or the most potential upside or risk.

Return JSON only:
{{
  "picks": [
    {{"netuid": <int>, "name": <string or "Unknown">, "reason": <one sentence why this is interesting>}},
    ...
  ],
  "deep_dive": <netuid of the single best subnet to explore further>
}}

Subnet data:
{overview}
"""


def _extract_json(text: str) -> dict:
    """Extract JSON object from model response, stripping code fences and surrounding text."""
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find outermost JSON object by scanning for { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON parse failed: {e}\nRaw response: {text[:600]}")
    raise ValueError(f"No JSON object found in response: {text[:400]}")


async def generate_watchlist(reader: ChainReader, claude) -> tuple[list[dict], int, str, bytes]:
    """Returns (picks, deep_dive_netuid, deep_dive_name, pdf_bytes)."""
    overview = await reader.subnet_overview()
    if not overview or overview.startswith("Error"):
        raise ValueError("Could not fetch subnet overview")

    result = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": WATCHLIST_PROMPT.format(overview=overview)}],
    )
    text = result.content[0].text.strip()

    try:
        parsed = _extract_json(text)
    except ValueError as e:
        raise ValueError(str(e))
    picks = parsed.get("picks", [])[:5]
    if not picks:
        raise ValueError("No picks returned")

    # Use model's recommended deep dive, fall back to first pick
    deep_dive_netuid = parsed.get("deep_dive")
    chosen = next((p for p in picks if p["netuid"] == deep_dive_netuid), picks[0])
    deep_netuid = chosen["netuid"]

    # Fetch full data for deep dive in parallel with identity lookups for all picks
    deep_memo, *identity_results = await asyncio.gather(
        generate(deep_netuid, reader, claude),
        *[reader.subnet_identity_summary(p["netuid"]) for p in picks],
    )

    # Enrich picks with identity names
    for i, pick in enumerate(picks):
        if not pick.get("name") or pick["name"].lower() == "unknown":
            identity = identity_results[i]
            name_match = re.search(r"Name:\s*(.+)", identity or "")
            if name_match:
                pick["name"] = name_match.group(1).strip()
            else:
                pick["name"] = f"SN{pick['netuid']}"

    deep_name = chosen.get("name") or f"SN{deep_netuid}"

    pdf_bytes = pdf_gen.generate_watchlist_pdf(picks, deep_netuid, deep_name, deep_memo)
    return picks, deep_netuid, deep_name, pdf_bytes


async def generate_pdf(netuid: int, reader: ChainReader, claude) -> tuple[str, bytes]:
    """Returns (subnet_name, pdf_bytes)."""
    memo_text = await generate(netuid, reader, claude)

    # Extract subnet name from identity or first line of memo
    identity = await reader.subnet_identity_summary(netuid)
    name_match = re.search(r"Name:\s*(.+)", identity or "")
    subnet_name = name_match.group(1).strip() if name_match else f"Subnet {netuid}"

    pdf_bytes = pdf_gen.generate_pdf(netuid, subnet_name, memo_text)
    return subnet_name, pdf_bytes


def split_for_telegram(text: str, limit: int = 4000) -> list[str]:
    """Split memo into chunks that fit Telegram's message limit."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return chunks
