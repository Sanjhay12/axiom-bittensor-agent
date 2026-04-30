import logging
import os
import httpx

BASE = "https://api.twitter.com/2"
TIMEOUT = 10
logger = logging.getLogger(__name__)


def _token() -> str:
    return os.getenv("TWITTER_BEARER_TOKEN", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def _available() -> bool:
    return bool(_token())


async def search_tweets(query: str, max_results: int = 10) -> list[dict]:
    if not _available():
        return []
    params = {
        "query": query + " -is:retweet lang:en",
        "max_results": max(10, min(max_results, 100)),
        "tweet.fields": "created_at,public_metrics",
        "expansions": "author_id",
        "user.fields": "name,username",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(f"{BASE}/tweets/search/recent", headers=_headers(), params=params)
        if r.status_code != 200:
            logger.warning(f"Twitter API {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        tweets = data.get("data", [])
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        result = []
        for t in tweets:
            user = users.get(t.get("author_id", ""), {})
            m = t.get("public_metrics", {})
            result.append({
                "text": t["text"].replace("\n", " "),
                "username": user.get("username", "?"),
                "created_at": (t.get("created_at") or "")[:10],
                "likes": m.get("like_count", 0),
                "retweets": m.get("retweet_count", 0),
            })
        return result
    except Exception as e:
        logger.warning(f"Twitter fetch failed: {e}")
        return []


def _format(tweets: list[dict], header: str) -> str:
    if not tweets:
        return ""
    lines = [header]
    for t in tweets:
        lines.append(
            f"  @{t['username']} ({t['created_at']}) "
            f"[♥{t['likes']} RT{t['retweets']}]: {t['text'][:220]}"
        )
    return "\n".join(lines)


async def subnet_tweets_context(netuid: int, name: str = "", handle: str = "") -> str:
    if not _available():
        return ""

    if handle:
        clean = handle.lstrip("@").split("/")[-1].strip()
        if clean:
            query = f"(from:{clean} OR @{clean})"
        else:
            handle = ""

    if not handle:
        terms = [f'"SN{netuid}"']
        if name and name.lower() not in ("unknown", f"sn{netuid}", f"subnet {netuid}"):
            terms.append(f'"{name}"')
        query = f"({' OR '.join(terms)}) bittensor"

    tweets = await search_tweets(query, max_results=10)
    label = name if name and name.lower() not in ("unknown",) else f"SN{netuid}"
    return _format(tweets, f"### SN{netuid} ({label}) — Recent Tweets")


async def bittensor_tweets_context() -> str:
    if not _available():
        return ""
    tweets = await search_tweets("(bittensor OR $TAO) -giveaway", max_results=15)
    return _format(tweets, "### Bittensor — Recent Twitter Activity")
