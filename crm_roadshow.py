"""
Roadshow planner — "I'm heading to LA with Nebari, who should I meet?"

Answers a city (optionally scoped to a product) with a tiered meeting plan:
  1. Anchors        — in/near the city, warm with several interactions, where an
                      in-person meeting is high-value.
  2. Meeting candidates — good prospects in the city worth an early meeting.
  3. Worth traveling for — strong prospects just outside the central city.

Location is the hard part: the CRM has no first-class city field historically, so
locations are sourced three ways and cached on the firm record — a manual owner tag
(authoritative, never overwritten), on-the-fly Claude inference (cached so the next
query is instant), and whatever firm research already turned up. Signal gathering is
deterministic SQL; the geographic tiering and the drafts are Claude.

List first, drafts on request: `roadshow LA` lists the meetings; `draft roadshow LA`
generates copy-paste meeting-request emails for the anchors and candidates.
"""
from __future__ import annotations
import json
import logging
import os
import time as _time

import psycopg2.extras
from anthropic import AsyncAnthropic

import store
import crm_store
import crm_draft

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"
DAY = 86400

MAX_CANDIDATES = 60   # cap the list handed to the tiering model
MAX_DRAFTS = 12       # cap emails generated in one draft-on-request pass


def _days_ago(ts, now):
    return max(0, (now - ts) // DAY) if ts else None


def _gather_candidates() -> list[dict]:
    """Worked or qualified contacts (not the raw cold import, not passed): anyone past
    New, or with a logged opportunity / deal size / importance / priority flag. These are
    the people actually worth planning a trip around; the 900+ untouched New imports are
    excluded until they've been worked."""
    with store.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.id, p.name, p.email, p.stage, p.importance, p.deal_amount_usd,
                       p.next_step, p.mandate, p.investor_type, p.role, p.relationship_type,
                       p.liked_products, p.warmth,
                       p.last_touch_ts, p.location AS person_location, p.notes,
                       f.id AS firm_id, f.name AS firm_name, f.location AS firm_location,
                       (SELECT COUNT(*) FROM crm_interactions i WHERE i.person_id = p.id) AS interaction_count,
                       (SELECT string_agg(DISTINCT o.product, '; ')
                          FROM crm_opportunities o WHERE o.person_id = p.id) AS products
                FROM crm_people p LEFT JOIN crm_firms f ON f.id = p.firm_id
                WHERE COALESCE(p.stage, 'New') <> 'Passed'
                  AND (
                    (p.stage IS NOT NULL AND p.stage <> 'New')
                    OR p.importance IS NOT NULL
                    OR p.deal_amount_usd IS NOT NULL
                    OR p.manual_priority
                    OR EXISTS (SELECT 1 FROM crm_opportunities o WHERE o.person_id = p.id)
                  )
                ORDER BY p.importance DESC NULLS LAST, p.last_touch_ts DESC NULLS LAST
                LIMIT %s
            """, (MAX_CANDIDATES,))
            return list(cur.fetchall())


def _effective_location(row: dict) -> str | None:
    """Contact's own city wins over firm HQ."""
    return row.get("person_location") or row.get("firm_location")


async def _infer_and_cache_locations(candidates: list[dict]) -> None:
    """For candidate firms with no location on file, ask Claude for each firm's HQ city in
    one call and cache the result (as 'inferred', so it never clobbers a manual tag). Updates
    the candidate dicts in place so the caller doesn't need to re-read."""
    missing = {}
    for r in candidates:
        if not _effective_location(r) and r.get("firm_id") and r.get("firm_name"):
            missing[r["firm_id"]] = r["firm_name"]
    if not missing:
        return

    firm_list = "\n".join(f"- {name}" for name in missing.values())
    prompt = (
        "For each investment firm / fund / family office below, give your BEST GUESS at its "
        "primary headquarters city. These are real firms a fundraiser deals with, so give a "
        "concrete city whenever you have any reasonable basis for one (the firm name, its known "
        "reputation, common domiciles for its strategy). Only return \"unknown\" if you truly "
        "have no basis at all. The guess is cached as inferred and the user can correct it, so "
        "prefer a plausible city over \"unknown\".\n\n"
        f"{firm_list}\n\n"
        "Return ONLY a JSON object mapping each firm name exactly as written to a string "
        "like \"Los Angeles, CA\", \"New York, NY\", \"London, UK\", or \"unknown\". No prose."
    )
    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip().strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        loc_map = json.loads(text)
    except Exception as e:
        logger.warning(f"crm_roadshow: location inference failed: {e}")
        return

    # name -> firm_id, to write the cache back
    name_to_id = {name: fid for fid, name in missing.items()}
    resolved = {}
    for name, loc in loc_map.items():
        if not loc or str(loc).strip().lower() == "unknown":
            continue
        fid = name_to_id.get(name)
        if fid is None:  # tolerate minor name drift from the model
            fid = next((i for n, i in name_to_id.items() if n.lower() == str(name).lower()), None)
        if fid is not None:
            crm_store.set_firm_location(fid, str(loc).strip(), source="inferred")
            resolved[fid] = str(loc).strip()

    for r in candidates:
        if not _effective_location(r) and r.get("firm_id") in resolved:
            r["firm_location"] = resolved[r["firm_id"]]


TIER_PROMPT = """You are planning an in-person fundraising roadshow for Joe Azzaro, a fund manager at Cedar Ridge Capital. He is traveling to a target city and wants to know which investors to meet.

You are given the target city, an optional product/fund he's raising for, and a list of qualified investor contacts with their location, pipeline stage, interaction count, and relationship signals.

Sort the relevant contacts into exactly three tiers. Include a contact in AT MOST one tier, and OMIT anyone whose location is unknown or clearly far from the target city (different region/coast).

CRITICAL — only potential INVESTORS belong on this roadshow. Cedar Ridge is RAISING capital, so OMIT any contact who is themselves a fund manager, GP, or asset manager running their own investment strategies — a peer/competitor who raises and deploys their own capital rather than an LP/allocator who could invest INTO Cedar Ridge's funds. Judge from their firm, role, and mandate: e.g. an asset manager running its own lending/credit strategies is a GP — omit it. Genuine allocators DO belong — family offices, pensions, endowments, funds-of-funds, OCIOs, consultants, RIAs/wealth managers allocating on behalf of clients — because they could allocate to Cedar Ridge. If it's genuinely unclear whether a contact is an allocator or a manager, leave them out.

- "anchors": located IN the target city or its immediate metro, already warm/engaged with roughly 2-3+ interactions, where an in-person meeting is high-value and timely. These are the priority meetings.
- "candidates": located IN the city or metro and worth meeting, but earlier-stage or fewer interactions than an anchor.
- "travel_worth": strong, worthwhile prospects located just OUTSIDE the central city (nearby city/suburb, same broad area) that could justify a short side-trip.

If a product is specified, prioritize contacts who fit it (liked products, live opportunities, mandate) and reference the fit in the reason.

For each contact include their exact email (as the identifier), name, firm, location, a one-line reason it's worth meeting (cite the real signal — stage, interactions, product fit), and a concrete suggested_ask for the meeting.

Return ONLY valid JSON, no prose:
{"anchors": [{"email": "", "name": "", "firm": "", "location": "", "reason": "", "suggested_ask": ""}], "candidates": [...], "travel_worth": [...], "summary": "one or two sentences on the shape of this trip"}

If no contacts are a geographic fit, return all three lists empty with a summary saying so."""


def _compact_candidates(candidates: list[dict], now: int) -> list[dict]:
    out = []
    for r in candidates:
        out.append({
            "email": r.get("email"), "name": r.get("name") or r.get("email"),
            "firm": r.get("firm_name"), "location": _effective_location(r) or "unknown",
            "stage": r.get("stage"), "interactions": r.get("interaction_count"),
            "warmth": r.get("warmth"), "importance": r.get("importance"),
            "products": r.get("products"), "liked_products": r.get("liked_products"),
            "mandate": r.get("mandate"), "investor_type": r.get("investor_type"),
            "role": r.get("role"), "relationship_type": r.get("relationship_type"),
            "days_since_touch": _days_ago(r.get("last_touch_ts"), now),
            "next_step": r.get("next_step"),
        })
    return out


async def plan(city: str, product: str | None = None) -> dict:
    """Returns {city, product, anchors, candidates, travel_worth, summary, candidate_count}.
    Infers+caches any missing firm locations first, then tiers with Claude."""
    now = int(_time.time())
    candidates = _gather_candidates()
    result = {"city": city, "product": product, "anchors": [], "candidates": [],
              "travel_worth": [], "fallback": [], "summary": "", "candidate_count": len(candidates)}
    if not candidates:
        result["summary"] = "No worked or qualified contacts on file yet to plan a trip around."
        return result

    await _infer_and_cache_locations(candidates)
    # Always compute a product-relevant target list as a fallback: if geo tiering finds nobody
    # in the city (thin/missing location data), the user still gets the investors to focus on
    # rather than a dead-end "no contacts" — see format_plan.
    result["fallback"] = _fallback_list(candidates, product, now)

    scope = f"\nProduct/fund in focus: {product}" if product else ""
    user = (
        f"Target city: {city}{scope}\n\n"
        f"Qualified contacts:\n{json.dumps(_compact_candidates(candidates, now), default=str)}"
    )
    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=1800,
            system=TIER_PROMPT + crm_store.directives_prompt_block(),
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text.strip().strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        data = json.loads(text)
        for k in ("anchors", "candidates", "travel_worth"):
            result[k] = data.get(k) or []
        result["summary"] = data.get("summary") or ""
    except Exception as e:
        logger.error(f"crm_roadshow: tiering failed: {e}")
        result["summary"] = f"Couldn't build the tiered plan ({e})."
    return result


def _fallback_list(candidates: list[dict], product: str | None, now: int) -> list[dict]:
    """Product-relevant target list used when geo tiering places nobody in the city. Prefers
    contacts whose products/liked-products/mandate mention the product; else the top qualified
    contacts. Ranked by importance then interaction depth."""
    prod = (product or "").lower().strip()
    prod_head = prod.split()[0] if prod else ""

    def relevant(r: dict) -> bool:
        if not prod:
            return False
        blob = " ".join(str(r.get(k) or "") for k in ("products", "liked_products", "mandate")).lower()
        return prod in blob or (prod_head and prod_head in blob)

    matched = [r for r in candidates if relevant(r)]
    pool = matched or candidates
    pool = sorted(pool, key=lambda r: (r.get("importance") or 0, r.get("interaction_count") or 0), reverse=True)
    return [{
        "name": r.get("name") or r.get("email"),
        "firm": r.get("firm_name"),
        "location": _effective_location(r) or "location unconfirmed",
        "stage": r.get("stage"),
        "product_fit": relevant(r),
    } for r in pool[:12]]


def _fmt_person(p: dict) -> str:
    loc = f" — {p['location']}" if p.get("location") else ""
    firm = f" ({p['firm']})" if p.get("firm") else ""
    lines = [f"&bull; <b>{p.get('name') or p.get('email')}</b>{firm}{loc}"]
    if p.get("reason"):
        lines.append(f"   {p['reason']}")
    if p.get("suggested_ask"):
        lines.append(f"   Ask: {p['suggested_ask']}")
    return "\n".join(lines)


def format_plan(p: dict) -> str:
    city = p["city"]
    scope = f" with {p['product']}" if p.get("product") else ""
    n = len(p["anchors"]) + len(p["candidates"]) + len(p["travel_worth"])
    if n == 0:
        fb = p.get("fallback") or []
        if not fb:
            return (
                f"<b>Roadshow — {city}{scope}</b>\n\n"
                "No worked or qualified investors on file yet to build a target list from."
            )
        # No geographic matches (usually thin location data) — give the product-relevant target
        # list anyway rather than dead-ending, and tell them how to sharpen it into an itinerary.
        prod = p.get("product")
        out = [
            f"<b>Roadshow — {city}{scope}</b>",
            f"I couldn't confirm any of your investors are based in {city} yet"
            + (" (locations aren't all tagged)." if any(x["location"] == "location unconfirmed" for x in fb)
               else " — your worked investors look concentrated elsewhere."),
            f"\n<b>Target list — {prod + '-relevant ' if prod else ''}investors to focus on ({len(fb)})</b>",
        ]
        for x in fb:
            firmp = f" ({x['firm']})" if x.get("firm") else ""
            loc = f" — {x['location']}" if x.get("location") else ""
            st = f" · {x['stage']}" if x.get("stage") else ""
            star = "  ★ fits" if x.get("product_fit") else ""
            out.append(f"&bull; <b>{x['name']}</b>{firmp}{loc}{st}{star}")
        out.append(
            f"\nRanked by priority. Tag where they're based — e.g. \"set location Nebari Partners: "
            f"New York\" — and I'll sort them into a {city} itinerary (anchors / candidates / worth "
            f"traveling for) and draft the meeting emails."
        )
        return "\n".join(x for x in out if x)
    out = [f"<b>Roadshow — {city}{scope}</b>", p.get("summary", "")]
    if p["anchors"]:
        out.append(f"\n<b>Anchors — meet these ({len(p['anchors'])})</b>")
        out += [_fmt_person(x) for x in p["anchors"]]
    if p["candidates"]:
        out.append(f"\n<b>Meeting candidates ({len(p['candidates'])})</b>")
        out += [_fmt_person(x) for x in p["candidates"]]
    if p["travel_worth"]:
        out.append(f"\n<b>Worth traveling for — just outside {city} ({len(p['travel_worth'])})</b>")
        out += [_fmt_person(x) for x in p["travel_worth"]]
    out.append(
        "\n<i>Locations are best-guess inferences unless you've tagged them — confirm before "
        "booking, and correct any with e.g. \"set location Banner Ridge: New York\".</i>"
    )
    out.append(
        f"Reply <b>draft roadshow {city}{scope}</b> to get copy-paste meeting-request "
        "emails for the anchors and candidates."
    )
    return "\n".join(x for x in out if x)


async def roadshow(city: str, product: str | None = None) -> str:
    return format_plan(await plan(city, product))


async def draft_emails(city: str, product: str | None = None) -> str:
    """Copy-paste meeting-request drafts for the anchors + candidates of a city plan."""
    p = await plan(city, product)
    people = (p["anchors"] or []) + (p["candidates"] or [])
    if not people:
        return format_plan(p)

    scope = f" with {product}" if product else ""
    instruction = (
        f"Write a short, warm email requesting an in-person meeting while Joe is visiting {city}"
        f"{scope}. Reference our existing relationship and any recent history, propose grabbing "
        "coffee or a short meeting during the visit, and keep it concise and easy to say yes to. "
        "Do not invent specific dates — leave the timing open or use a placeholder."
    )
    blocks = [f"<b>Draft meeting emails — {city}{scope}</b>",
              "Copy, tweak, and send. One per prospect:\n"]
    for person in people[:MAX_DRAFTS]:
        email = person.get("email")
        label = person.get("name") or email or "contact"
        try:
            draft = await crm_draft.generate(email or label, instruction)
        except Exception as e:
            draft = f"(couldn't draft this one: {e})"
        blocks.append(f"<b>— {label} ({person.get('firm') or ''})</b>\n{draft}")
    if len(people) > MAX_DRAFTS:
        blocks.append(f"(+{len(people) - MAX_DRAFTS} more — narrow with a product scope to trim the list.)")
    return "\n\n".join(blocks)
