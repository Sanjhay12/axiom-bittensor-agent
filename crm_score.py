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


def rank_active_people() -> list[dict]:
    """Every active person merged with their score — used by the radar digest and ranking views."""
    people = crm_store.list_active_people()
    results = []
    for p in people:
        interactions = crm_store.list_interactions(p["id"])
        scored = lp_score(p, interactions)
        results.append({**p, **scored})
    return sorted(results, key=lambda r: r["composite_score"], reverse=True)
