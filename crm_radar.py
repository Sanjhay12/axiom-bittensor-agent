"""
Phase 2 — Follow-Up Radar. Daily digest of who to follow up with, who's gone cold,
and which conversations have open loops. Pure DB query + template — no LLM call needed,
since the underlying data (stage, last_touch_ts, next_step) is already structured.
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import crm_mail
import crm_score

logger = logging.getLogger(__name__)

COLD_AFTER_DAYS = 14
HIGH_SCORE_THRESHOLD = 70
DIGEST_HOUR_UTC = 8  # 8am UTC daily
MAX_ITEMS = 8


def _days_since(ts: int | None) -> int | None:
    if not ts:
        return None
    return int((time.time() - ts) / 86400)


def build_digest() -> str | None:
    ranked = crm_score.rank_active_people()
    if not ranked:
        return None

    flagged = []
    for p in ranked:
        days = _days_since(p.get("last_touch_ts"))
        is_cold = days is not None and days >= COLD_AFTER_DAYS
        has_open_loop = bool(p.get("next_step"))
        is_high_score = (p.get("composite_score") or 0) >= HIGH_SCORE_THRESHOLD
        if not (is_cold or has_open_loop or is_high_score):
            continue
        flagged.append((p, days, is_cold, has_open_loop))

    if not flagged:
        return None

    flagged = flagged[:MAX_ITEMS]  # already sorted by composite_score desc from rank_active_people

    lines = [f"<b>Follow-Up Radar — {datetime.now(timezone.utc).strftime('%b %d')}</b>", ""]
    for i, (p, days, is_cold, has_open_loop) in enumerate(flagged, 1):
        name = p.get("name") or p["email"]
        firm = f" at {p['firm_name']}" if p.get("firm_name") else ""
        days_str = f"last touched {days}d ago" if days is not None else "no recorded touch"
        flag = "cold" if is_cold else ("open loop" if has_open_loop else "high LP score")
        line = f"{i}. <b>{name}{firm}</b> — score {p['composite_score']}/100, {days_str}, {flag}"
        if p.get("next_step"):
            line += f"\n   Next step: {p['next_step']}"
        lines.append(line)

    return "\n".join(lines)


async def send_digest():
    digest = build_digest()
    if digest:
        await crm_mail.send_async(f"Follow-Up Radar — {datetime.now(timezone.utc).strftime('%b %d')}", digest)
    else:
        logger.info("crm_radar: nothing to send today")


async def run_daily_loop():
    logger.info("crm_radar: daily follow-up loop started")
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=DIGEST_HOUR_UTC, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            await send_digest()
        except Exception as e:
            logger.error(f"crm_radar: digest failed: {e}")
