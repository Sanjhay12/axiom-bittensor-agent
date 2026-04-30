import logging
from datetime import datetime, timezone
import httpx

BASE = "https://www.reddit.com"
TIMEOUT = 10
HEADERS = {"User-Agent": "AxiomBot/1.0 (Bittensor research agent; personal use)"}
logger = logging.getLogger(__name__)


def _age(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _fmt(posts: list[dict], header: str) -> str:
    if not posts:
        return ""
    lines = [header]
    for p in posts:
        body = f" — {p['body']}" if p.get("body") else ""
        lines.append(
            f"  [{p['score']}up {p['comments']}comments] {p['date']} u/{p['author']}: {p['title']}{body}"
        )
    return "\n".join(lines)


async def _fetch(url: str, params: dict) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            logger.warning(f"Reddit {r.status_code}: {url}")
            return []
        children = r.json().get("data", {}).get("children", [])
        posts = []
        for c in children:
            d = c["data"]
            if d.get("stickied"):
                continue
            text = (d.get("selftext") or "").strip()
            posts.append({
                "title": d["title"][:120],
                "score": d.get("score", 0),
                "comments": d.get("num_comments", 0),
                "author": d.get("author", "?"),
                "date": _age(d.get("created_utc", 0)),
                "body": text[:200] if text and text != "[removed]" else "",
            })
        return posts
    except Exception as e:
        logger.warning(f"Reddit fetch failed: {e}")
        return []


async def subnet_reddit_context(netuid: int, name: str = "") -> str:
    label = name if name and name.lower() not in ("unknown",) else f"SN{netuid}"
    terms = [f"SN{netuid}"]
    if name and name.lower() not in ("unknown", f"sn{netuid}", f"subnet {netuid}"):
        terms.append(name)
    query = " OR ".join(f'"{t}"' for t in terms)

    posts = await _fetch(
        f"{BASE}/r/bittensor_/search.json",
        {"q": query, "sort": "new", "restrict_sr": "1", "t": "month", "limit": 10},
    )
    return _fmt(posts, f"### SN{netuid} ({label}) — Recent Reddit Posts (r/bittensor_)")


async def bittensor_reddit_context() -> str:
    posts = await _fetch(
        f"{BASE}/r/bittensor_/hot.json",
        {"limit": 15, "t": "week"},
    )
    return _fmt(posts, "### Bittensor — Hot Reddit Posts (r/bittensor_)")
