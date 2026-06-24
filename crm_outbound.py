"""
Phase 7 — Outbound lead discovery. Explicitly the lowest-priority phase per the plan —
the wedge is the warm relationship graph, not cold scraping. This is a separate,
optional daily job: searches for new prospects matching the ICP via Perplexity web
search and delivers a Telegram digest. Requires PERPLEXITY_API_KEY; no-ops without it.
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

import crm_mail
import crm_store

logger = logging.getLogger(__name__)

PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
DISCOVERY_HOUR_UTC = 9

ICP_QUERY = """Find 5 specific people or firms that match this ICP for a private credit fund \
raising capital: placement agents, emerging fund managers, fundraisers, boutique investment \
banks, independent sponsors, capital advisors, or RIAs/family offices with a stated interest in \
private credit. For each, give: name, firm, role, and a one-sentence reason they fit. Only return \
real, specific, currently-findable people/firms — never invent one. If you can't find 5 with real \
sourcing, return fewer."""


async def discover() -> list[dict]:
    if not PERPLEXITY_API_KEY:
        logger.info("crm_outbound: no PERPLEXITY_API_KEY configured, skipping")
        return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={"Authorization": f"Bearer {PERPLEXITY_API_KEY}"},
                json={"model": "sonar", "messages": [{"role": "user", "content": ICP_QUERY}]},
            )
            if r.status_code != 200:
                logger.warning(f"crm_outbound: perplexity returned {r.status_code}")
                return []
            content = r.json()["choices"][0]["message"]["content"]
            return [{"raw": content}]
    except Exception as e:
        logger.error(f"crm_outbound: discovery failed: {e}")
        return []


async def run_discovery_once():
    results = await discover()
    if not results:
        return
    text = results[0]["raw"]
    crm_store.create_lead(name=None, firm_name=None, notes=text, source="outbound_discovery")
    await crm_mail.send_async("Outbound Discovery — new prospects", text)


async def run_daily_loop():
    if not PERPLEXITY_API_KEY:
        logger.info("crm_outbound: PERPLEXITY_API_KEY not set, loop will idle and no-op")
    logger.info("crm_outbound: daily discovery loop started")
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=DISCOVERY_HOUR_UTC, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            await run_discovery_once()
        except Exception as e:
            logger.error(f"crm_outbound: loop error: {e}")
