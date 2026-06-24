"""
Phase 6 — Contact enrichment (delayed, runs after a brand-new contact is created).
Best-effort: any provider without a configured API key is skipped, never raises.
"""
from __future__ import annotations
import logging
import os

import httpx

import crm_store

logger = logging.getLogger(__name__)

PROXYCURL_API_KEY = os.getenv("PROXYCURL_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
CRUNCHBASE_API_KEY = os.getenv("CRUNCHBASE_API_KEY")


async def _linkedin(email_addr: str, name: str | None) -> dict:
    if not PROXYCURL_API_KEY or not name:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://nubela.co/proxycurl/api/v2/linkedin/profile/resolve",
                params={"first_name": name, "email": email_addr},
                headers={"Authorization": f"Bearer {PROXYCURL_API_KEY}"},
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(f"crm_enrich: proxycurl failed for {email_addr}: {e}")
    return {}


async def _news(name: str | None, firm_name: str | None) -> dict:
    if not PERPLEXITY_API_KEY or not (name or firm_name):
        return {}
    query = f"Recent news about {name or ''} {firm_name or ''} in the last 6 months. Be concise."
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {PERPLEXITY_API_KEY}"},
                json={
                    "model": "sonar",
                    "messages": [{"role": "user", "content": query}],
                },
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"]["content"]
                return {"summary": content}
    except Exception as e:
        logger.warning(f"crm_enrich: perplexity news failed for {name}/{firm_name}: {e}")
    return {}


async def _funding(firm_name: str | None) -> dict:
    if not CRUNCHBASE_API_KEY or not firm_name:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://api.crunchbase.com/api/v4/autocompletes",
                params={"query": firm_name, "collection_ids": "organizations"},
                headers={"X-cb-user-key": CRUNCHBASE_API_KEY},
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(f"crm_enrich: crunchbase failed for {firm_name}: {e}")
    return {}


async def enrich_person(person_id: int, extracted: dict):
    """Fire-and-forget enrichment for a newly created contact. Skips providers with no API key."""
    name = extracted.get("person_name")
    firm_name = extracted.get("firm_name")
    email_addr = extracted.get("person_email") or ""

    if not any([PROXYCURL_API_KEY, PERPLEXITY_API_KEY, CRUNCHBASE_API_KEY]):
        logger.info("crm_enrich: no enrichment API keys configured, skipping")
        return

    linkedin, news, funding = await _linkedin(email_addr, name), await _news(name, firm_name), await _funding(firm_name)
    data = {"linkedin": linkedin, "news": news, "funding": funding}
    if any(data.values()):
        crm_store.set_enrichment(person_id, data)
        logger.info(f"crm_enrich: enriched person {person_id}")
