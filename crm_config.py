"""
Level 2 "config" — the owner sets bounded, typed knobs by email.

Unlike Level 1 directives (freeform behaviour injected into prompts), these are a
fixed set of validated settings that the deterministic code reads via
crm_store.get_config* with a default fallback: digest hour, digest recipients,
and the cold/stale/overdue thresholds. Email "config" to list, or a plain request
like "send my digest at 12:00 UTC" / "flag contacts cold after 14 days".
"""
from __future__ import annotations
import json
import logging
import os
import re

from anthropic import AsyncAnthropic

import crm_store

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-sonnet-4-5"

# The only settable knobs. Code reads each with the matching default elsewhere.
CONFIG_SPEC = {
    "digest_hour_utc":        {"type": "int",  "min": 0, "max": 23, "desc": "hour (0-23, UTC) the daily follow-up digest sends"},
    "digest_recipients":      {"type": "enum", "values": ["primary", "all"], "desc": "who receives the daily digest: 'primary' owner only, or 'all' owners"},
    "cold_days":              {"type": "int",  "min": 1, "max": 365, "desc": "days without contact before a relationship counts as going cold"},
    "stale_opp_days":         {"type": "int",  "min": 1, "max": 365, "desc": "days without an update before an opportunity counts as stale"},
    "reply_overdue_days":     {"type": "int",  "min": 1, "max": 90,  "desc": "days an inbound email can sit unanswered before it's overdue"},
    "follow_up_overdue_days": {"type": "int",  "min": 1, "max": 90,  "desc": "days a set next-step can sit before the follow-up is overdue"},
}

_LIST_RE = re.compile(r"^\s*(?:list\s+|show\s+)?(?:config|settings|configuration)\s*\??\s*$", re.I)


def _spec_for_prompt() -> str:
    out = []
    for k, v in CONFIG_SPEC.items():
        rng = f"allowed: {v['values']}" if v["type"] == "enum" else f"integer {v['min']}-{v['max']}"
        out.append(f"- {k}: {v['desc']} ({rng})")
    return "\n".join(out)


CLASSIFY_PROMPT = """You map a fund manager's message to a CONFIG change for his CRM, if it is one.

The ONLY settable config keys are:
{spec}

Interpret natural phrasing and convert timezones to UTC:
- "send my digest at 7am Eastern" -> digest_hour_utc = 11 (Eastern is UTC-4 in summer)
- "email the daily digest to both of us" -> digest_recipients = all
- "flag contacts as cold after 2 weeks" -> cold_days = 14

If the message is NOT asking to change one of these specific settings (it's a question, a tone/format instruction, a data update, chit-chat), return is_config = false.

Return ONLY JSON, no prose: {{"is_config": true|false, "key": "<one key or null>", "value": "<new value or null>"}}"""


def _validate(key: str, value) -> tuple[str | None, str | None]:
    """Returns (clean_value, None) on success or (None, error_message)."""
    spec = CONFIG_SPEC.get(key)
    if not spec:
        return None, "unknown setting"
    if spec["type"] == "int":
        try:
            v = int(str(value).strip())
        except (TypeError, ValueError):
            return None, "not a whole number"
        if not (spec["min"] <= v <= spec["max"]):
            return None, f"must be between {spec['min']} and {spec['max']}"
        return str(v), None
    if spec["type"] == "enum":
        v = str(value).strip().lower()
        if v not in spec["values"]:
            return None, f"must be one of {spec['values']}"
        return v, None
    return None, "unhandled type"


async def try_config_command(note: str, from_owner: bool) -> str | None:
    note = note.strip()

    if _LIST_RE.match(note):
        cfg = crm_store.get_all_config()
        lines = "\n".join(f"  {k} = {cfg.get(k, '(default)')}" for k in CONFIG_SPEC)
        return f"<b>Config</b>\n{lines}\n\n(e.g. email \"send my digest at 12:00 UTC\" to change one.)"

    if not from_owner:
        return None

    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=200,
            system=CLASSIFY_PROMPT.format(spec=_spec_for_prompt()),
            messages=[{"role": "user", "content": note}],
        )
        text = resp.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        data = json.loads(text)
    except Exception as e:
        logger.error(f"crm_config: classify failed: {e}")
        return None

    if not data.get("is_config") or not data.get("key"):
        return None
    key = data["key"]
    if key not in CONFIG_SPEC:
        return None
    value, err = _validate(key, data.get("value"))
    if err:
        return f"Couldn't set {key}: {err}."
    crm_store.set_config(key, value, None)
    return f"Done — <b>{key}</b> is now <b>{value}</b>."
