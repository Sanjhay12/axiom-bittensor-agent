import os
import re
import httpx

TIMEOUT = 10
MAX_README_CHARS = 3000  # trim long READMEs before injecting into context


def _gh_headers() -> dict:
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

_GITHUB_RE = re.compile(r'github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)', re.IGNORECASE)


_PLACEHOLDER_OWNERS = {"username", "user", "owner", "yourname", "example", "placeholder"}

def _extract_repo(url: str) -> str | None:
    """Extract 'owner/repo' from any GitHub URL."""
    m = _GITHUB_RE.search(url)
    if not m:
        return None
    repo = m.group(1).rstrip("/").removesuffix(".git")
    owner = repo.split("/")[0].lower()
    if owner in _PLACEHOLDER_OWNERS:
        return None
    return repo


async def fetch_readme(github_url: str) -> str | None:
    repo = _extract_repo(github_url)
    if not repo:
        return None

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{repo}/{branch}/README.md"
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    text = r.text.strip()
                    if len(text) > MAX_README_CHARS:
                        text = text[:MAX_README_CHARS] + "\n... (truncated)"
                    return text
            except Exception:
                continue

    return None


async def fetch_repo_meta(github_url: str) -> dict:
    repo = _extract_repo(github_url)
    if not repo:
        return {}

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            r = await client.get(
                f"https://api.github.com/repos/{repo}",
                headers=_gh_headers(),
            )
            if r.status_code == 200:
                data = r.json()
                return {
                    "stars": data.get("stargazers_count"),
                    "forks": data.get("forks_count"),
                    "open_issues": data.get("open_issues_count"),
                    "last_push": data.get("pushed_at", "")[:10],
                    "description": data.get("description") or "",
                }
        except Exception:
            pass

    return {}


async def fetch_commits(github_url: str, limit: int = 5) -> list[dict]:
    repo = _extract_repo(github_url)
    if not repo:
        return []

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/commits",
                params={"per_page": limit},
                headers=_gh_headers(),
            )
            if r.status_code == 200:
                return [
                    {
                        "sha": c["sha"][:7],
                        "message": c["commit"]["message"].splitlines()[0][:100],
                        "author": c["commit"]["author"]["name"],
                        "date": c["commit"]["author"]["date"][:10],
                    }
                    for c in r.json()
                ]
        except Exception:
            pass

    return []


async def fetch_pull_requests(github_url: str, limit: int = 5) -> list[dict]:
    repo = _extract_repo(github_url)
    if not repo:
        return []

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/pulls",
                params={"state": "open", "per_page": limit, "sort": "updated"},
                headers=_gh_headers(),
            )
            if r.status_code == 200:
                return [
                    {
                        "number": p["number"],
                        "title": p["title"][:100],
                        "author": p["user"]["login"],
                        "updated": p["updated_at"][:10],
                        "draft": p.get("draft", False),
                    }
                    for p in r.json()
                ]
        except Exception:
            pass

    return []


async def fetch_releases(github_url: str, limit: int = 3) -> list[dict]:
    repo = _extract_repo(github_url)
    if not repo:
        return []

    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        try:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/releases",
                params={"per_page": limit},
                headers=_gh_headers(),
            )
            if r.status_code == 200:
                return [
                    {
                        "tag": rel["tag_name"],
                        "name": rel["name"][:80] if rel.get("name") else rel["tag_name"],
                        "published": rel["published_at"][:10] if rel.get("published_at") else "",
                        "prerelease": rel.get("prerelease", False),
                    }
                    for rel in r.json()
                ]
        except Exception:
            pass

    return []
