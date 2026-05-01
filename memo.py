import asyncio
import json
import re
import time
from chain import ChainReader, fetch_tao_price
import pdf_gen

_cache: dict = {}
MEMO_TTL = 3 * 3600  # 3 hours

MEMO_PROMPT = """\
You are writing a Bittensor subnet research memo. This is not a data dump — it is a research piece that tells a story. Each section should build on the previous one. The memo should paint a complete picture: what the subnet is trying to do, whether it is doing it, whether it is worth engaging with, and exactly how — depending on who you are.

Use NO markdown, NO bold with **, NO asterisks, NO # headers. Write each section label in ALL CAPS on its own line, then write the content below it. Never put the section label and content on the same line.

OVERVIEW
One tight paragraph. What problem does this subnet solve in the real world? What is the actual incentive mechanism — what are miners being paid to do, and how is their work verified on-chain? How does this compare to what centralised players (AWS, OpenAI, Google, etc.) or competing subnets are doing in the same space? This sets the thesis. Everything else in the memo either supports or undermines it.

NETWORK HEALTH
Output only key-value pairs, one per line, in exactly this format: Label: Value
Include: Neurons, Registration Cost, Tempo, Immunity Period, Active Validators, Active Miners
Example line: Neurons: 201 / 256

COMPETITIVE LANDSCAPE
How does this subnet compare to the most similar subnets on Bittensor? How does it compare to centralised or off-chain alternatives doing the same thing? Where is it winning, where is it losing, and what is its actual moat if any? Be specific — name competitors.

VALIDATOR LANDSCAPE
Do not list validators. Interpret what the stake distribution means. Is the network healthy or dangerously centralised? What does the vtrust distribution say about consensus quality? Are there signs of gaming or collusion? End with a clear one-sentence view on whether this is a good place to stake right now.
Validator Score: x/10

MINER LANDSCAPE
What does it actually take to compete here? What hardware is required, what is the setup difficulty, what is the realistic expected revenue range for a new miner entering today? Are incentive scores differentiated or clustered (and what does that mean)? Remember: miner TAO emission is zero by design in dTAO — focus on incentive, alpha earnings, and competitiveness. End with a clear one-sentence view on mining viability.
Miner Score: x/10

EMISSION & ECONOMICS
Follow the money. What is total emission, how does it flow, what does a validator realistically earn per day at current rates? Is the emission level justified by the subnet's traction and quality? What is the risk/reward for capital deployed here? If TAO price is available, translate emissions into USD context.
Economics Score: x/10

DEVELOPMENT ACTIVITY
Do not list commits. Tell the story of what this team has actually built and where they are heading. What does the nature of recent work say about team maturity — are they building real infrastructure, shipping features, or doing optics maintenance? What does GitHub momentum (stars, forks, open issues, PR velocity) say about community traction? What has been shipped recently that matters?
Development Score: x/10

RISK FACTORS
What are the key risks to this subnet's execution — not data flags, but things that could actually break the thesis? Consider: stake centralisation risk, competitive displacement, technical delivery risk, incentive mechanism fragility, team/operational risk. Be direct.
Risk Score: x/10

VERDICT
One honest paragraph. Is this subnet investable, worth mining, worth validating, or worth avoiding right now? Give a clear call to action. Do not hedge.
Overall Score: x/10

RECOMMENDATIONS
Write exactly three lines, each starting with the role label:
Miners: one sentence — should they mine here, why or why not, any hardware or timing notes.
Validators: one sentence — should they stake here, why or why not.
Investors/Holders: one sentence — is there a capital allocation case, what is the thesis.

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
        max_tokens=3000,
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

    identity = await reader.subnet_identity_summary(netuid)
    name_match = re.search(r"Name:\s*(.+)", identity or "")
    subnet_name = name_match.group(1).strip() if name_match else f"Subnet {netuid}"

    # Short tagline from identity description
    desc_match = re.search(r"Description:\s*(.+)", identity or "")
    tagline = None
    if desc_match:
        desc = desc_match.group(1).strip()
        tagline = desc if len(desc) <= 80 else desc[:77] + "..."

    pdf_bytes = pdf_gen.generate_pdf(netuid, subnet_name, memo_text, tagline=tagline)
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
