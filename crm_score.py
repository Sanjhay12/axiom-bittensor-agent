"""
LP scoring — same architecture as risk_score.py: weighted signals, each normalized
to 0-100, with a confidence flag so missing data just drops out of the weighted
average instead of dragging the composite toward zero. Replaces Claude's single-shot
"importance" guess with an explicit, auditable score per contact.
"""
from __future__ import annotations
import json
import re
import time
from collections import namedtuple

import crm_store

Signal = namedtuple("Signal", ["score", "confidence"])

STAGE_SCORES = {
    "New": 10, "Contacted": 20, "Engaged": 40, "Intro made": 45,
    "Materials sent": 55, "Call scheduled": 65, "Diligence": 80,
    "Soft circled": 90, "Committed": 100, "Passed": 0, "Dormant": 15,
}

SENTIMENT_SCORES = {"positive": 100, "neutral": 50, "urgent": 70, "negative": 10}

RECENCY_WINDOW_DAYS = 60
DEAL_SIZE_RE = re.compile(r"\$?\s?(\d+(?:\.\d+)?)\s*([mMkK])\b")


def _stage_progress(person: dict, interactions: list[dict]) -> Signal:
    stage = person.get("stage")
    if not stage or stage not in STAGE_SCORES:
        return Signal(0, 0)
    return Signal(STAGE_SCORES[stage], 1)


def _recency(person: dict, interactions: list[dict]) -> Signal:
    last_touch = person.get("last_touch_ts")
    if not last_touch:
        return Signal(0, 0)
    days = (time.time() - last_touch) / 86400
    score = max(0.0, 100 - (days / RECENCY_WINDOW_DAYS) * 100)
    return Signal(round(score, 1), 1)


def _engagement_depth(person: dict, interactions: list[dict]) -> Signal:
    if not interactions:
        return Signal(0, 0)
    count_score = min(100, len(interactions) * 15)
    importances = [i["importance"] for i in interactions if i.get("importance") is not None]
    avg_importance_score = (sum(importances) / len(importances)) * 20 if importances else count_score
    return Signal(round((count_score + avg_importance_score) / 2, 1), 1)


def _sentiment_trend(person: dict, interactions: list[dict]) -> Signal:
    scored = [SENTIMENT_SCORES[i["sentiment"]] for i in interactions if i.get("sentiment") in SENTIMENT_SCORES]
    if not scored:
        return Signal(0, 0)
    # most recent interactions first (list_interactions orders DESC) — weight recency-decayed
    weights = [1 / (idx + 1) for idx in range(len(scored))]
    weighted = sum(s * w for s, w in zip(scored, weights)) / sum(weights)
    return Signal(round(weighted, 1), 1)


def _deal_size(person: dict, interactions: list[dict]) -> Signal:
    # Prefer the structured field Claude extracts directly; fall back to regex
    # scanning free text only for older records that predate that field.
    structured = person.get("deal_amount_usd")
    if structured:
        millions = structured / 1_000_000
        return Signal(round(min(100, millions * 10), 1), 1)

    text = " ".join(filter(None, [
        person.get("mandate"), person.get("notes"),
        *[i.get("summary") or "" for i in interactions],
    ]))
    matches = DEAL_SIZE_RE.findall(text)
    if not matches:
        return Signal(0, 0)
    amounts_m = []
    for num, unit in matches:
        val = float(num)
        amounts_m.append(val if unit.lower() == "m" else val / 1000)
    biggest = max(amounts_m)
    # $1M -> ~20, $5M -> ~60, $10M+ -> 100
    score = min(100, biggest * 10)
    return Signal(round(score, 1), 1)


def _fund_history(person: dict, interactions: list[dict]) -> Signal:
    raw = person.get("enrichment")
    if not raw:
        return Signal(0, 0)
    try:
        enrichment = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return Signal(0, 0)
    if not enrichment.get("funding"):
        return Signal(0, 0)
    return Signal(70, 1)  # presence of real funding-history data; refine once enrichment ships real fields


SIGNALS = [
    ("stage_progress",   _stage_progress,   0.30),
    ("recency",          _recency,          0.20),
    ("engagement_depth", _engagement_depth, 0.20),
    ("deal_size",        _deal_size,        0.15),
    ("sentiment_trend",  _sentiment_trend,  0.10),
    ("fund_history",     _fund_history,     0.05),
]


def lp_score(person: dict, interactions: list[dict]) -> dict:
    breakdown = {}
    composite = 0.0
    total_weight = 0.0

    for label, fn, weight in SIGNALS:
        signal = fn(person, interactions)
        if signal.confidence == 0:
            breakdown[label] = None
            continue
        breakdown[label] = signal.score
        composite += signal.score * weight
        total_weight += weight

    if total_weight > 0:
        composite = round(composite / total_weight, 1)

    return {
        "person_id": person.get("id"),
        "composite_score": composite,
        "breakdown": breakdown,
    }


def score_by_query(query: str) -> dict | None:
    person = crm_store.find_person(query)
    if not person:
        return None
    interactions = crm_store.list_interactions(person["id"])
    result = lp_score(person, interactions)
    result["name"] = person.get("name") or person["email"]
    result["firm_name"] = person.get("firm_name")
    return result


def _opp_stage_progress(opp: dict) -> Signal:
    stage = opp.get("stage")
    if not stage or stage not in STAGE_SCORES:
        return Signal(0, 0)
    return Signal(STAGE_SCORES[stage], 1)


def _opp_momentum(opp: dict) -> Signal:
    if not opp.get("next_step"):
        return Signal(0, 0)
    return Signal(100, 1)


def _opp_deal_size(opp: dict) -> Signal:
    structured = opp.get("deal_amount_usd")
    if not structured:
        return Signal(0, 0)
    millions = structured / 1_000_000
    return Signal(round(min(100, millions * 10), 1), 1)


def _opp_objection_health(opp: dict) -> Signal:
    """Fraction of raised objections that are marked resolved — a deal with every
    objection resolved scores high, one with several still open scores low."""
    profile = opp.get("objection_profile")
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except (json.JSONDecodeError, TypeError):
            profile = None
    active = [k for k, v in (profile or {}).items() if v]
    if not active:
        return Signal(0, 0)
    resolutions = opp.get("objection_resolutions")
    if isinstance(resolutions, str):
        try:
            resolutions = json.loads(resolutions)
        except (json.JSONDecodeError, TypeError):
            resolutions = {}
    resolutions = resolutions or {}
    resolved = sum(1 for k in active if resolutions.get(k))
    return Signal(round((resolved / len(active)) * 100, 1), 1)


def _opp_investor_breadth(investor_count: int) -> Signal:
    """More than one investor actively engaged on the same deal is a real signal —
    it means organizational traction, not just one person's interest."""
    if investor_count <= 0:
        return Signal(0, 0)
    return Signal(round(min(100, investor_count * 40), 1), 1)


OPP_SIGNALS = [
    ("stage_progress", _opp_stage_progress, 0.40),
    ("momentum", _opp_momentum, 0.15),
    ("deal_size", _opp_deal_size, 0.15),
    ("objection_health", _opp_objection_health, 0.15),
]


def opportunity_score(opp: dict, investor_count: int = 1) -> dict:
    """Same weighted-signal architecture as lp_score, but for one opportunity/product —
    lets the radar and pipeline views score a deal on its own merits (stage, momentum,
    deal size, objection resolution, how many investors are actually engaged on it)
    instead of only ever scoring the people attached to it."""
    breakdown = {}
    composite = 0.0
    total_weight = 0.0

    for label, fn, weight in OPP_SIGNALS:
        signal = fn(opp)
        if signal.confidence == 0:
            breakdown[label] = None
            continue
        breakdown[label] = signal.score
        composite += signal.score * weight
        total_weight += weight

    breadth = _opp_investor_breadth(investor_count)
    if breadth.confidence:
        breakdown["investor_breadth"] = breadth.score
        composite += breadth.score * 0.15
        total_weight += 0.15
    else:
        breakdown["investor_breadth"] = None

    if total_weight > 0:
        composite = round(composite / total_weight, 1)

    return {"composite_score": composite, "breakdown": breakdown}


_LP_SIGNAL_LABELS = {
    "stage_progress": "Stage in the pipeline (how far along: New → Contacted → Engaged → … → Committed)",
    "recency": f"Recency of last contact (100 if touched today, decaying to 0 at {RECENCY_WINDOW_DAYS} days)",
    "engagement_depth": "Engagement depth (how many interactions, and their average importance)",
    "deal_size": "Deal size (~$1M → 20, $5M → 60, $10M+ → 100)",
    "sentiment_trend": "Sentiment trend of recent interactions (recency-weighted)",
    "fund_history": "Fund history (whether enrichment found real funding-history data)",
}
_OPP_SIGNAL_LABELS = {
    "stage_progress": "Stage of the deal",
    "momentum": "Momentum (whether there's an open next step)",
    "deal_size": "Deal size",
    "objection_health": "Objection health (share of raised objections that are resolved)",
    "investor_breadth": "Investor breadth (how many investors are actively engaged on it)",
}


def explain_methodology() -> str:
    """Human-readable description of exactly how contacts and funds are scored, generated
    from the live SIGNALS/OPP_SIGNALS weights so it can never drift out of sync with the
    code. Routed to whenever the owner asks 'how do you score …'."""
    def _pct(w: float) -> str:
        return f"{int(round(w * 100))}%"

    stages = ", ".join(f"{k} {v}" for k, v in STAGE_SCORES.items())
    lp_lines = "\n".join(
        f"  • {_LP_SIGNAL_LABELS.get(label, label)} — <b>{_pct(weight)}</b>"
        for label, _fn, weight in SIGNALS
    )
    opp_weights = {label: weight for label, _fn, weight in OPP_SIGNALS}
    opp_weights["investor_breadth"] = 0.15  # added dynamically in opportunity_score()
    opp_lines = "\n".join(
        f"  • {_OPP_SIGNAL_LABELS.get(label, label)} — <b>{_pct(weight)}</b>"
        for label, weight in opp_weights.items()
    )
    return (
        "<b>How I score</b>\n"
        "Every contact and every fund gets a 0–100 composite: a weighted average of signals, "
        "each scored 0–100. A signal with no data drops out entirely (it doesn't drag the score "
        "down), so the composite is only over the signals I actually have.\n\n"
        "<b>LP / contact score</b>\n"
        f"{lp_lines}\n\n"
        "<b>Fund / opportunity score</b> (this is what a fund's score is)\n"
        f"{opp_lines}\n\n"
        f"<b>Stage values used above:</b> {stages}.\n\n"
        "Ask <b>score &lt;name&gt;</b> for any contact's exact composite plus the per-signal breakdown."
    )


def rank_active_people() -> list[dict]:
    """Every active person merged with their score — used by the radar digest and ranking views."""
    people = crm_store.list_active_people()
    interactions_by_person = crm_store.list_interactions_for_people([p["id"] for p in people])
    results = []
    for p in people:
        scored = lp_score(p, interactions_by_person.get(p["id"], []))
        results.append({**p, **scored})
    return sorted(results, key=lambda r: r["composite_score"], reverse=True)
