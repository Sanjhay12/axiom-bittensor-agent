import asyncio
import logging
import os
from datetime import datetime, timezone
import httpx

DISCORD_API = "https://discord.com/api/v10"
TIMEOUT = 10
logger = logging.getLogger(__name__)

# Text channels we care about — matched by name substring
CHANNEL_KEYWORDS = [
    "announcement", "general", "subnet", "mining", "validator",
    "governance", "news", "alpha", "research", "discussion",
]
MAX_CHANNELS = 3
MESSAGES_PER_CHANNEL = 25


def _token() -> str:
    return os.getenv("DISCORD_TOKEN", "")


def _guild_id() -> str:
    return os.getenv("DISCORD_GUILD_ID", "")


def _headers() -> dict:
    return {
        "Authorization": _token(),
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _available() -> bool:
    return bool(_token()) and bool(_guild_id())


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.rstrip("Z")).replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


async def _get(client: httpx.AsyncClient, path: str):
    try:
        r = await client.get(f"{DISCORD_API}{path}", headers=_headers(), timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"Discord API {r.status_code}: {path}")
        return None
    except Exception as e:
        logger.warning(f"Discord fetch failed: {e}")
        return None


async def _pick_channels(client: httpx.AsyncClient) -> list[dict]:
    channels = await _get(client, f"/guilds/{_guild_id()}/channels")
    if not channels:
        return []

    # Keep only text channels (type 0), score by keyword relevance
    scored = []
    for ch in channels:
        if ch.get("type") != 0:
            continue
        name = (ch.get("name") or "").lower()
        score = sum(1 for kw in CHANNEL_KEYWORDS if kw in name)
        if score > 0:
            scored.append((score, ch["id"], ch["name"]))

    scored.sort(reverse=True)
    return [{"id": cid, "name": name} for _, cid, name in scored[:MAX_CHANNELS]]


async def bittensor_discord_context() -> str:
    if not _available():
        return ""

    async with httpx.AsyncClient(follow_redirects=True) as client:
        channels = await _pick_channels(client)
        if not channels:
            return ""

        sections = []
        for i, ch in enumerate(channels):
            if i > 0:
                await asyncio.sleep(0.5)  # gentle pacing between channel reads

            msgs = await _get(client, f"/channels/{ch['id']}/messages?limit={MESSAGES_PER_CHANNEL}")
            if not msgs:
                continue

            lines = [f"  #{ch['name']}"]
            for m in msgs:
                author = m.get("author", {}).get("username", "?")
                content = (m.get("content") or "").replace("\n", " ").strip()
                if not content or m.get("type") not in (0, 19):  # normal + reply only
                    continue
                date = _fmt_ts(m.get("timestamp", ""))
                lines.append(f"    [{date}] {author}: {content[:200]}")

            if len(lines) > 1:
                sections.append("\n".join(lines))

    if not sections:
        return ""

    header = "### Bittensor Discord — Recent Messages"
    return header + "\n" + "\n\n".join(sections)
