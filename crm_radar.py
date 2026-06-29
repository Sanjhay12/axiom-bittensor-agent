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
import crm_store

logger = logging.getLogger(__name__)

COLD_AFTER_DAYS = 14
HIGH_SCORE_THRESHOLD = 70
DIGEST_HOUR_UTC = 8  # 8am UTC daily
MAX_ITEMS = 12

CATEGORY_MAP = {
    "lp_prospect": "Investors",
    "founder": "Funds",
    "consultant": "Advisors",
    "advisor": "Advisors",
    "intro": "Intros",
    "lead": "Leads",
}
CATEGORY_ORDER = ["Investors", "Funds", "Advisors", "Intros", "Leads", "Other"]


def _days_since(ts: int | None) -> int | None:
    if not ts:
        return None
    return int((time.time() - ts) / 86400)


def _category(person: dict) -> str:
    return CATEGORY_MAP.get(person.get("relationship_type"), "Other")


def build_digest() -> str | None:
    ranked = crm_score.rank_active_people()
    by_id = {p["id"]: p for p in ranked}

    # Manually flagged contacts always show up, even if cold/score logic wouldn't catch them
    # (e.g. already past pipeline, or just hasn't accumulated enough signal yet).
    for p in crm_store.list_manual_priority_people():
        if p["id"] not in by_id:
            scored = crm_score.lp_score(p, crm_store.list_interactions(p["id"]))
            by_id[p["id"]] = {**p, **scored}

    flagged = []
    for p in by_id.values():
        days = _days_since(p.get("last_touch_ts"))
        is_cold = days is not None and days >= COLD_AFTER_DAYS
        has_open_loop = bool(p.get("next_step"))
        is_high_score = (p.get("composite_score") or 0) >= HIGH_SCORE_THRESHOLD
        is_manual = bool(p.get("manual_priority"))
        if not (is_cold or has_open_loop or is_high_score or is_manual):
            continue
        flagged.append((p, days, is_cold, has_open_loop, is_manual))

    if not flagged:
        return None

    flagged.sort(key=lambda t: (not t[4], -(t[0].get("composite_score") or 0)))
    flagged = flagged[:MAX_ITEMS]

    grouped: dict[str, list] = {}
    for item in flagged:
        grouped.setdefault(_category(item[0]), []).append(item)

    lines = [f"<b>Follow-Up Radar — {datetime.now(timezone.utc).strftime('%b %d')}</b>", ""]
    for category in CATEGORY_ORDER:
        if category not in grouped:
            continue
        lines.append(f"<b>{category}</b>")
        for p, days, is_cold, has_open_loop, is_manual in grouped[category]:
            name = p.get("name") or p["email"]
            firm = f" at {p['firm_name']}" if p.get("firm_name") else ""
            days_str = f"last touched {days}d ago" if days is not None else "no recorded touch"
            channel = f"via {p.get('contact_channel') or 'email'}"
            flag = "manually flagged" if is_manual else ("cold" if is_cold else ("open loop" if has_open_loop else "high LP score"))
            line = f"  <b>{name}{firm}</b> — score {p.get('composite_score', 0)}/100, {days_str} ({channel}), {flag}"
            if p.get("next_step"):
                line += f"\n    Next step: {p['next_step']}"
            lines.append(line)
        lines.append("")

    return "\n".join(lines).strip()


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
