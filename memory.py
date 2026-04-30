import json
import os
from anthropic import AsyncAnthropic

_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
MEMORY_FILE = os.path.join(_DATA_DIR, "memory.json")

_DEFAULT = {
    "user_facts": [],
    "watched_subnets": [],
    "watched_hotkeys": [],
    "preferences": [],
}

_EXTRACT_PROMPT = """\
You are a memory extraction assistant. Given a conversation exchange, extract anything worth remembering about the user long-term.

Current memory:
{memory}

Last exchange:
User: {user_msg}
Assistant: {assistant_msg}

Extract updates as JSON with these optional keys (only include keys with genuinely new info):
- "user_facts": list of strings — things the user said about themselves (role, subnets they run, their setup)
- "watched_subnets": list of ints — subnet IDs they clearly care about (asked about repeatedly or said so explicitly)
- "watched_hotkeys": list of strings — SS58 hotkeys they asked about
- "preferences": list of strings — how they want responses (tone, format, what data they care about)

Rules:
- Do not add info already in memory
- For watched_subnets, only add if they mentioned it multiple times or explicitly said they care about it
- Return null if nothing new to save

Respond with JSON only, no explanation."""


def load() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                data = json.load(f)
                for key in _DEFAULT:
                    data.setdefault(key, [])
                return data
        except Exception:
            pass
    return {k: list(v) for k, v in _DEFAULT.items()}


def save(mem: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, indent=2)


def to_prompt(mem: dict) -> str:
    lines = []
    if mem.get("user_facts"):
        lines.append("About this user: " + "; ".join(mem["user_facts"]))
    if mem.get("watched_subnets"):
        lines.append("Subnets they care about: " + ", ".join(f"SN{n}" for n in mem["watched_subnets"]))
    if mem.get("watched_hotkeys"):
        lines.append("Hotkeys they track: " + ", ".join(mem["watched_hotkeys"]))
    if mem.get("preferences"):
        lines.append("Their preferences: " + "; ".join(mem["preferences"]))
    return "\n".join(lines)


async def maybe_update(claude: AsyncAnthropic, mem: dict, user_msg: str, assistant_msg: str) -> dict:
    prompt = _EXTRACT_PROMPT.format(
        memory=json.dumps(mem, indent=2),
        user_msg=user_msg,
        assistant_msg=assistant_msg,
    )
    try:
        result = await claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = result.content[0].text.strip()
        if not text or text.lower() == "null":
            return mem

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        updates = json.loads(text)
        if not updates:
            return mem

        changed = False
        for key in ("user_facts", "preferences"):
            for item in updates.get(key, []):
                if item not in mem[key]:
                    mem[key].append(item)
                    changed = True

        for netuid in updates.get("watched_subnets", []):
            if isinstance(netuid, int) and netuid not in mem["watched_subnets"]:
                mem["watched_subnets"].append(netuid)
                changed = True

        for hk in updates.get("watched_hotkeys", []):
            if isinstance(hk, str) and hk not in mem["watched_hotkeys"]:
                mem["watched_hotkeys"].append(hk)
                changed = True

        if changed:
            save(mem)

    except Exception:
        pass

    return mem
