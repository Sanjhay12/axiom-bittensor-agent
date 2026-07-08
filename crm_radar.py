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

    opps_by_person = crm_store.list_opportunities_for_people(list(by_id.keys()))

    flagged = []
    for p in by_id.values():
        days = _days_since(p.get("last_touch_ts"))
        is_cold = days is not None and days >= COLD_AFTER_DAYS
        has_open_loop = bool(p.get("next_step"))
        is_high_score = (p.get("composite_score") or 0) >= HIGH_SCORE_THRESHOLD
        is_manual = bool(p.get("manual_priority"))

        opps = opps_by_person.get(p["id"], [])
        opp_next_steps = [o for o in opps if o.get("next_step")]
        opp_objections = [o for o in opps if o.get("objections")]

        if not (is_cold or has_open_loop or is_high_score or is_manual or opp_next_steps or opp_objections):
            continue
        flagged.append((p, days, is_cold, has_open_loop, is_manual, opp_next_steps, opp_objections))

    if not flagged:
        return None

    flagged.sort(key=lambda t: (not t[4], -(t[0].get("composite_score") or 0)))
    flagged = flagged[:MAX_ITEMS]

    # Fund-manager/GP-side contacts (relationship_type "founder") are reporting on their
    # OWN fund, not a personal relationship being cultivated — Joe wants one line per
    # FUND, not one per employee (e.g. Fairbridge's two IR contacts shouldn't produce two
    # separate lines). They're rolled into a single "Funds" section below, one per firm,
    # instead of appearing in the per-person category list.
    founder_items = [item for item in flagged if item[0].get("relationship_type") == "founder"]
    flagged = [item for item in flagged if item[0].get("relationship_type") != "founder"]

    grouped: dict[str, list] = {}
    for item in flagged:
        grouped.setdefault(_category(item[0]), []).append(item)

    lines = [f"<b>Follow-Up Radar — {datetime.now(timezone.utc).strftime('%b %d')}</b>", ""]
    for category in CATEGORY_ORDER:
        if category not in grouped:
            continue
        lines.append(f"<b>{category}</b>")
        for p, days, is_cold, has_open_loop, is_manual, opp_next_steps, opp_objections in grouped[category]:
            name = p.get("name") or p["email"]
            firm = f" at {p['firm_name']}" if p.get("firm_name") else ""
            days_str = f"last touched {days}d ago" if days is not None else "no recorded touch"
            channel = f"via {p.get('contact_channel') or 'email'}"
            reasons = []
            if is_manual:
                reasons.append("manually flagged")
            if is_cold:
                reasons.append("cold")
            if has_open_loop:
                reasons.append("open loop")
            if opp_next_steps:
                reasons.append("opportunity next step")
            if opp_objections:
                reasons.append("unresolved objection")
            if not reasons:
                reasons.append("high LP score")
            line = f"  <b>{name}{firm}</b> — score {p.get('composite_score', 0)}/100, {days_str} ({channel}), {', '.join(reasons)}"
            if p.get("next_step"):
                line += f"\n    Next step: {p['next_step']}"
            seen_opp_ids = set()
            for o in (opp_next_steps or []) + (opp_objections or []):
                if o["id"] in seen_opp_ids:
                    continue
                seen_opp_ids.add(o["id"])
                if o.get("next_step"):
                    line += f"\n    {o['product']} next step: {o['next_step']}"
                if o.get("objections"):
                    line += f"\n    {o['product']} objection: {o['objections']}"
            lines.append(line)
        lines.append("")

    if founder_items:
        lines.append("<b>Funds</b>")
        by_firm: dict = {}
        for p, *_rest, opp_next_steps, opp_objections in founder_items:
            firm_key = p.get("firm_id") or p.get("firm_name") or p["id"]
            entry = by_firm.setdefault(firm_key, {"firm_name": p.get("firm_name") or "Unknown firm", "opps": {}})
            # Keyed by product name, not opportunity row id — each employee at the same
            # fund gets their OWN opportunity row for the same product (opportunities are
            # keyed on person + product), so de-duping by id would still show the same
            # fund/product twice when two employees are both tied to it.
            for o in (opp_next_steps or []) + (opp_objections or []):
                existing = entry["opps"].get(o["product"])
                if existing is None:
                    entry["opps"][o["product"]] = o
                else:
                    if not existing.get("next_step") and o.get("next_step"):
                        existing["next_step"] = o["next_step"]
                    if not existing.get("objections") and o.get("objections"):
                        existing["objections"] = o["objections"]

        def _firm_score(entry):
            opps = list(entry["opps"].values())
            if not opps:
                return 0
            # investor_count=0 disables the investor-breadth signal here — this section
            # scores fund/deal status, not how many of the fund's own staff emailed.
            return max(crm_score.opportunity_score(o, 0)["composite_score"] for o in opps)

        for entry in sorted(by_firm.values(), key=_firm_score, reverse=True):
            opps = list(entry["opps"].values())
            lines.append(f"  <b>{entry['firm_name']}</b> — score {_firm_score(entry)}/100")
            for o in opps:
                sub = f"    {o['product']} ({o.get('stage') or 'New'})"
                if o.get("next_step"):
                    sub += f": {o['next_step']}"
                lines.append(sub)
                if o.get("objections"):
                    lines.append(f"    {o['product']} objection: {o['objections']}")
        lines.append("")

    body = "\n".join(lines).strip()
    return body or None


async def send_digest():
    digest = build_digest()
    if not digest:
        logger.info("crm_radar: nothing to send today")
        return
    subject = f"Follow-Up Radar — {datetime.now(timezone.utc).strftime('%b %d')}"
    # digest_recipients: "all" (default) sends to every owner; "primary" only the first.
    if crm_store.get_config("digest_recipients", "all") == "all" and crm_mail.OWNER_EMAILS:
        for owner in crm_mail.OWNER_EMAILS:
            await crm_mail.send_async(subject, digest, to=owner)
    else:
        await crm_mail.send_async(subject, digest)


async def run_daily_loop():
    logger.info("crm_radar: daily follow-up loop started")
    while True:
        now = datetime.now(timezone.utc)
        hour = crm_store.get_config_int("digest_hour_utc", DIGEST_HOUR_UTC)
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            await send_digest()
        except Exception as e:
            logger.error(f"crm_radar: digest failed: {e}")
