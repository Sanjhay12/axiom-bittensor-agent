import asyncio
import json
import re
import time
from datetime import datetime, timezone
from chain import ChainReader, fetch_tao_price
import pdf_gen

_cache: dict = {}
MEMO_TTL = 3 * 3600  # 3 hours

MEMO_PROMPT = """\
You are writing a Bittensor subnet research memo for Cedar Ridge Capital. This is not a report template — it is a research piece that builds a single, coherent argument. Think of it as a mosaic: each section contributes a piece, and together they should paint a complete picture. The reader should finish with a clear view of what this subnet is, whether it is working, and exactly what they should do.

The through-line is always: what is this subnet trying to accomplish, is it succeeding, and what does that mean for each type of participant right now?

Critical rules:
- Use NO markdown, NO bold with **, NO asterisks, NO # headers.
- Write each section label in ALL CAPS on its own line, then write the content below it. Never put the section label and content on the same line.
- Do not treat sections as independent silos. Each section should reference and build on what came before. A reader should feel the argument tightening as they read.
- When historical data is provided in the context, use it to describe trends — not just the current state. Trends are often more informative than point-in-time numbers.
- Every section that asks for a score: give it honestly. Scores should be defensible from the content above them.

OVERVIEW
One tight paragraph. Name the real-world problem this subnet is solving. Describe the incentive mechanism concretely — what are miners being paid to do, and how is their work scored and verified? Situate it competitively: who are the centralised players in this space (AWS, OpenAI, Hugging Face, etc.) and which Bittensor subnets are competing for the same ground? This paragraph sets the thesis. Everything that follows either supports or challenges it.

NETWORK HEALTH
Output key-value pairs only, one per line, in exactly this format: Label: Value
Include only fields where real data is available — omit any field entirely if the value is unknown, missing, or N/A. Do not output placeholder rows.
If no data at all is available for this subnet, output a single line: Status: Data unavailable — metagraph fetch failed
Fields to include when available: Neurons, Registration Cost, Tempo, Immunity Period, Active Validators, Active Miners, Total Stake, Alpha Price, Data As Of
Example line: Neurons: 201 / 256

MARKET POSITION
Analyse where this subnet sits competitively — do not list competitors, argue a position. How does it compare to the most similar subnets on Bittensor? Name them and be specific about where this one wins and where it does not. Then go wider: how does it stack up against centralised or off-chain alternatives doing the same thing? If any concrete performance metrics are available — tokens processed, compute delivered, requests served, throughput — use them and compare. What is the actual moat, if one exists? If historical data is in context, say whether the competitive position has strengthened or eroded over the observation window.

DEVELOPMENT ACTIVITY
Tell the story of what this team has built and where they are heading — not what commits were merged. Avoid mentioning CI/CD pipelines, dependency bumps, build tooling, or routine maintenance. The reader wants to understand product direction and team maturity: what real capabilities have been shipped, what milestones have been hit, and what does the pattern of work say about how serious this team is? What does GitHub traction — stars, forks, community engagement — say about external conviction? If activity data across time is available, say whether momentum is accelerating, holding, or declining. End with a plain statement of whether this team appears to be building something durable.
Development Score: x/10

NETWORK DYNAMICS
Read the network as a system. What does the validator stake distribution reveal about who controls consensus — genuinely distributed or effectively concentrated? What does vtrust say about consensus quality in practice? On the miner side: what does it actually take to compete here — hardware, setup complexity, realistic time to first revenue? Are incentive scores spread across the field or clustered at the top? If historical data is available, describe how concentration, churn, or participation has shifted. Connect the validator and miner pictures — do they tell a consistent story about network health or do they contradict each other?
Network Score: x/10

ECONOMICS
Follow the money and translate it into terms a participant can act on. What is total emission, where does it flow, and what does a validator or miner realistically earn per day at current rates? Estimate a credible revenue range for a new entrant — include hardware cost context where relevant. Is the emission level justified by actual subnet traction and quality, or is protocol subsidy doing the heavy lifting? If TAO price is available, give USD context. If historical emission or alpha price data is in context, describe the trend and what it implies about capital flows. Flag any divergence between emission growth and alpha price — that divergence is usually the most important signal.
Economics Score: x/10

RISK FACTORS
Challenge the thesis from the overview directly. Do not produce a checklist — write about the two or three risks that feel most live for this specific subnet given everything above. Name the specific mechanism by which each risk could materialise, not just the category it falls into. Stake centralisation, competitive displacement, delivery risk, mechanism fragility, team depth — whatever is genuinely relevant here, say why it is a real concern rather than a theoretical one.
Risk Score: x/10

VERDICT
One honest paragraph. Is this subnet investable, worth mining, worth validating, or worth avoiding right now? If it is compelling for some participant types but not others, say so clearly. Give a concrete call to action grounded in the argument above. Do not hedge. Do not qualify everything into neutrality.
Overall Score: x/10

DECISION GUIDE
Three short paragraphs, one per audience type. Each opens with a direct signal — Buy / Mine / Validate / Hold / Avoid — followed by the reasoning in two or three sentences. These are not summaries of the memo; they are the practical conclusion for someone with 30 seconds to make a decision.

Miners: Open with the signal. Specify hardware requirement, estimated setup difficulty, expected revenue range or time to breakeven, and whether now is the right entry window.
Validators: Open with the signal. Say whether this is a good staking destination right now, what the return expectation is, and what stake risk looks like given concentration dynamics.
Investors: Open with the signal. State the capital allocation thesis if one exists, name the catalysts that would validate it, and say what would change the view.

WHAT CHANGED
Write this section only if historical snapshot data appears in the context. Compare current state to the earliest available data point across the key metrics: alpha price, emission, neuron count, registration cost, GitHub activity, validator concentration. Frame changes in terms of what they imply — not just what the numbers show. If nothing material has changed, say so in one sentence.

SOURCE APPENDIX
List each data source used. One line per source in this format: Source: Description (timestamp if available)
Include: on-chain data fetch time, GitHub data fetch time, TAO price source and time, historical snapshot range if applicable.

---
Live Data and Historical Context:
{context}
"""


def _fetch_historical_context(netuid: int) -> str:
    """Pull 30d/90d snapshots from the DB and format them as context for the prompt."""
    try:
        import store
        now = int(time.time())
        history = store.get_subnet_history(netuid, days=90)
        alpha_hist = store.get_alpha_price_history(netuid, days=90)
        churn_hist = store.get_churn_history(netuid, days=90)
        gh_hist = store.get_github_history(netuid, days=90)

        if not history:
            return ""

        def _snap_near(ts_target, window=7 * 86400):
            return next(
                (s for s in reversed(history)
                 if abs(s["ts"] - ts_target) <= window),
                None,
            )

        snap_now = history[-1]
        snap_30d = _snap_near(now - 30 * 86400)
        snap_90d = _snap_near(now - 90 * 86400)

        lines = ["### Historical Snapshot Data"]

        def _fmt_snap(label, snap, ref=None):
            ts = datetime.fromtimestamp(snap["ts"], timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"\n{label} (snapshot: {ts}):")
            fields = [
                ("alpha_price_tao", "Alpha price", "{:.6f} TAO"),
                ("total_emission_tao", "Total emission", "{:.4f} TAO"),
                ("reg_cost_tao", "Registration cost", "{:.4f} TAO"),
                ("neuron_count", "Neurons", "{}"),
                ("validator_count", "Validators", "{}"),
                ("total_stake_tao", "Total stake", "{:.2f} TAO"),
            ]
            for key, label_str, fmt in fields:
                val = snap.get(key)
                if val is None:
                    continue
                line = f"  {label_str}: {fmt.format(val)}"
                if ref and ref.get(key) and key in ("alpha_price_tao", "total_emission_tao", "reg_cost_tao"):
                    ref_val = ref[key]
                    if ref_val:
                        pct = (val - ref_val) / ref_val * 100
                        line += f" ({pct:+.1f}% vs now)"
                lines.append(line)

        _fmt_snap("Current", snap_now)
        if snap_30d and snap_30d["ts"] != snap_now["ts"]:
            _fmt_snap("~30 days ago", snap_30d, ref=snap_now)
        if snap_90d and snap_90d["ts"] != snap_now["ts"]:
            _fmt_snap("~90 days ago", snap_90d, ref=snap_now)

        # Alpha price trend (last 6 data points)
        if len(alpha_hist) >= 2:
            lines.append("\nAlpha price series (recent):")
            for row in alpha_hist[-6:]:
                dt = datetime.fromtimestamp(row["ts"], timezone.utc).strftime("%Y-%m-%d")
                lines.append(f"  {dt}: {row['alpha_price_tao']:.6f} TAO")

        # Churn summary
        if churn_hist:
            rates = [c["churn_rate"] for c in churn_hist if c.get("churn_rate") is not None]
            if rates:
                avg = sum(rates) / len(rates)
                lines.append(f"\nChurn: avg {avg:.1%} of neurons turning over per cycle across {len(rates)} recorded events")

        # GitHub trend
        if len(gh_hist) >= 2:
            gh_new, gh_old = gh_hist[-1], gh_hist[0]
            gh_new_dt = datetime.fromtimestamp(gh_new["ts"], timezone.utc).strftime("%Y-%m-%d")
            gh_old_dt = datetime.fromtimestamp(gh_old["ts"], timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"\nGitHub trend ({gh_old_dt} → {gh_new_dt}):")
            if gh_new.get("stars") and gh_old.get("stars"):
                lines.append(f"  Stars: {gh_old['stars']} → {gh_new['stars']} ({gh_new['stars'] - gh_old['stars']:+d})")
            if gh_new.get("commits_30d") is not None:
                lines.append(f"  Commits (last 30d, latest snapshot): {gh_new['commits_30d']}")
            if gh_old.get("commits_30d") is not None:
                lines.append(f"  Commits (last 30d, earliest snapshot): {gh_old['commits_30d']}")

        return "\n".join(lines)
    except Exception:
        return ""


def _fetch_chart_data(netuid: int) -> dict:
    """Fetch time-series data for chart rendering in the PDF."""
    try:
        import store
        alpha = store.get_alpha_price_history(netuid, days=90)
        subnet_hist = store.get_subnet_history(netuid, days=90)
        churn_hist = store.get_churn_history(netuid, days=90)
        return {
            "alpha_price": [(r["ts"], r["alpha_price_tao"]) for r in alpha if r.get("alpha_price_tao")],
            "emission": [(r["ts"], r["total_emission_tao"]) for r in subnet_hist if r.get("total_emission_tao")],
            "reg_cost": [(r["ts"], r["reg_cost_tao"]) for r in subnet_hist if r.get("reg_cost_tao")],
            "neuron_count": [(r["ts"], r["neuron_count"]) for r in subnet_hist if r.get("neuron_count")],
            "churn_rate": [(r["ts"], r["churn_rate"]) for r in churn_hist if r.get("churn_rate")],
        }
    except Exception:
        return {}


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

    live_context = "\n\n".join(p for p in [detail, metagraph, github, identity, price] if p)
    if not live_context:
        return f"No data available for SN{netuid} right now."

    historical = _fetch_historical_context(netuid)
    context = live_context + ("\n\n" + historical if historical else "")

    result = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
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
    memo_text, identity = await asyncio.gather(
        generate(netuid, reader, claude),
        reader.subnet_identity_summary(netuid),
    )

    name_match = re.search(r"Name:\s*(.+)", identity or "")
    subnet_name = name_match.group(1).strip() if name_match else f"Subnet {netuid}"

    desc_match = re.search(r"Description:\s*(.+)", identity or "")
    tagline = None
    if desc_match:
        desc = desc_match.group(1).strip()
        tagline = desc if len(desc) <= 80 else desc[:77] + "..."

    chart_data = _fetch_chart_data(netuid)
    pdf_bytes = pdf_gen.generate_pdf(netuid, subnet_name, memo_text, tagline=tagline, chart_data=chart_data)
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
