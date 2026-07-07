"""
Phase 6 — Contact enrichment (delayed, runs after a brand-new contact is created,
whether from an inbound email or a bulk spreadsheet import), and on-demand via the
"enrich_request" free-form command.
Best-effort: LinkedIn is skipped without a configured Proxycurl key; news and
funding history use Claude's own web_search tool rather than a separate search
API, so they run on the existing ANTHROPIC_API_KEY with no extra account needed.
"""
from __future__ import annotations
import logging
import os

import httpx
from anthropic import AsyncAnthropic

import crm_store

logger = logging.getLogger(__name__)

PROXYCURL_API_KEY = os.getenv("PROXYCURL_API_KEY")

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}


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


async def _web_search_summary(query: str) -> str | None:
    """Runs one Claude + web_search turn and returns the final text answer, or None
    on failure/no result — enrichment is best-effort and shouldn't raise or block."""
    try:
        resp = await claude.messages.create(
            model=MODEL,
            max_tokens=500,
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": query}],
        )
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        return text or None
    except Exception as e:
        logger.warning(f"crm_enrich: web_search failed for query '{query[:60]}': {e}")
        return None


async def _news(name: str | None, firm_name: str | None) -> dict:
    if not (name or firm_name):
        return {}
    query = (
        f"Search the web for recent news about {name or ''} {firm_name or ''} in the last 6 "
        f"months. Be concise and specific. If you find nothing concrete, say so plainly rather "
        f"than guessing."
    )
    summary = await _web_search_summary(query)
    return {"summary": summary} if summary else {}


async def _funding(firm_name: str | None) -> dict:
    if not firm_name:
        return {}
    query = (
        f"Search the web for {firm_name}'s funding history — funding rounds, amounts raised, "
        f"investors, AUM if it's a fund. Be concise and specific. If you can't find anything "
        f"concrete, say so plainly rather than guessing."
    )
    summary = await _web_search_summary(query)
    return {"summary": summary} if summary else {}


async def enrich_person(person_id: int, extracted: dict):
    """Fire-and-forget enrichment for a contact — called from the inbound email path,
    the spreadsheet import path, and on-demand via the "enrich_request" free-form
    command. LinkedIn is skipped without a configured Proxycurl key; news/funding
    always attempt since they only need the existing ANTHROPIC_API_KEY."""
    name = extracted.get("person_name")
    firm_name = extracted.get("firm_name")
    email_addr = extracted.get("person_email") or ""

    linkedin, news, funding = await _linkedin(email_addr, name), await _news(name, firm_name), await _funding(firm_name)
    data = {"linkedin": linkedin, "news": news, "funding": funding}
    if any(data.values()):
        crm_store.set_enrichment(person_id, data)
        logger.info(f"crm_enrich: enriched person {person_id}")


async def enrich_firm(firm_id: int, firm_name: str) -> dict:
    """Researches a fund/firm directly — for when the user wants background on a fund
    that has no contact on file yet (e.g. "research Acme Capital's funding history"
    with no known person there). No LinkedIn lookup since that's person-specific.
    Returns the data saved (empty dict if nothing came back)."""
    news, funding = await _news(None, firm_name), await _funding(firm_name)
    data = {"news": news, "funding": funding}
    if any(data.values()):
        crm_store.set_firm_enrichment(firm_id, data)
        logger.info(f"crm_enrich: enriched firm {firm_id}")
    return data
