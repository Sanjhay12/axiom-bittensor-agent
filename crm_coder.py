"""
Level 3 — the owner changes the CRM's CODE by email (direct-change mode).

An owner emails a code-change request that INCLUDES the shared secret; a Claude
coding loop (Opus 4.8, tool-use) edits the crm_* source, the change is test-gated,
and only on green is it committed to main via the GitHub API (Railway auto-deploys)
and the diff emailed back. Per the owner's choice this is direct (no PR).

Deployed containers have no local git repo, so the commit is made through the GitHub
Git Data API (blobs -> tree -> commit -> update ref) using GITHUB_TOKEN — no `.git`
needed. Edited files are written to the (ephemeral) container fs only so tests can run;
nothing persists unless the API commit succeeds.

Guardrails:
  1. Shared secret in the email (CRM_CODE_SECRET) — the real auth AND the trigger, since
     From: is spoofable. No secret -> never touches code.
  2. Tests must pass (py_compile of changed files + test_crm.py) before the commit.
  3. The unified diff is emailed back to the requester (audit trail).
Only crm_* source files are editable — never .env, trading code, infra, or this file.

Deploy env: CRM_CODE_SECRET (passphrase) and GITHUB_TOKEN (PAT, Contents:write).
"""
from __future__ import annotations
import difflib
import logging
import os
import re
import subprocess

import httpx
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-opus-4-8"

REPO = os.path.dirname(os.path.abspath(__file__))
CODE_SECRET = os.getenv("CRM_CODE_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GH_OWNER, GH_REPO, GH_BRANCH = "Sanjhay12", "axiom-bittensor-agent", "main"
GH_API = f"https://api.github.com/repos/{GH_OWNER}/{GH_REPO}"

_EDITABLE_RE = re.compile(r"^crm_[a-z0-9_]+\.py$")
_PROTECTED = {"crm_coder.py", "crm_mail.py"}  # coder can't rewrite its own auth path
MAX_STEPS = 24

SYSTEM = """You are a careful software engineer editing a Python fundraising-CRM codebase from an emailed request by its owner. Make the smallest change that fully satisfies the request.

Rules:
- Only edit crm_*.py files (never .env, trading code, or infra). Use list_files to see what's editable.
- read_file before write_file. write_file replaces the whole file, so read it, change only what's needed, and write the complete new contents.
- Match the surrounding code's style. Keep imports valid. Don't break other features.
- When done, stop (end your turn). Do not commit or push — the harness runs tests and deploys.
- If the request is unsafe, unclear, or outside the crm_* surface, do nothing and explain why in your final message."""

TOOLS = [
    {"name": "list_files", "description": "List the editable crm_* source files.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "read_file", "description": "Read a source file's full contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"], "additionalProperties": False}},
    {"name": "write_file", "description": "Overwrite an editable crm_* file with new full contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"], "additionalProperties": False}},
]


def has_secret(text: str) -> bool:
    return bool(CODE_SECRET) and bool(text) and CODE_SECRET in text


def _safe_name(path: str) -> str | None:
    name = os.path.basename((path or "").strip())
    if name != (path or "").strip().lstrip("./"):
        return None
    if not _EDITABLE_RE.match(name) or name in _PROTECTED:
        return None
    return name


def _run_tool(name: str, inp: dict, edits: dict) -> tuple[str, bool]:
    """edits maps filename -> {'original': str, 'content': str} for changed files."""
    if name == "list_files":
        files = sorted(f for f in os.listdir(REPO) if _EDITABLE_RE.match(f) and f not in _PROTECTED)
        return "\n".join(files), False
    if name == "read_file":
        fn = _safe_name(inp.get("path", ""))
        p = os.path.join(REPO, fn) if fn else None
        if not p or not os.path.exists(p):
            return f"Cannot read {inp.get('path')!r}: not an editable crm_* file.", True
        with open(p, encoding="utf-8") as f:
            return f.read(), False
    if name == "write_file":
        fn = _safe_name(inp.get("path", ""))
        if not fn:
            return f"Refused: {inp.get('path')!r} is not an editable crm_* file.", True
        p = os.path.join(REPO, fn)
        if fn not in edits:  # capture the original once, for diff + tests
            with open(p, encoding="utf-8") as f:
                edits[fn] = {"original": f.read()}
        content = inp.get("content", "")
        edits[fn]["content"] = content
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {fn}.", False
    return f"Unknown tool {name}", True


def _tests_pass(changed: list[str]) -> tuple[bool, str]:
    """Rail 2: compile every changed file; run test_crm.py if present."""
    for fn in changed:
        r = subprocess.run(["python", "-m", "py_compile", os.path.join(REPO, fn)], capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"{fn} does not compile:\n{r.stderr[-800:]}"
    if os.path.exists(os.path.join(REPO, "test_crm.py")):
        r = subprocess.run(["python", "-m", "pytest", "-q", "test_crm.py"], cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"test_crm.py failed:\n{(r.stdout + r.stderr)[-1200:]}"
    return True, ""


def _restore_originals(edits: dict):
    for fn, e in edits.items():
        with open(os.path.join(REPO, fn), "w", encoding="utf-8") as f:
            f.write(e["original"])


async def _commit_via_github(edits: dict, message: str) -> tuple[bool, str]:
    """Atomic multi-file commit to main via the GitHub Git Data API. No local git needed."""
    if not GITHUB_TOKEN:
        return False, "GITHUB_TOKEN not set — can't push."
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        async with httpx.AsyncClient(base_url=GH_API, headers=headers, timeout=30) as gh:
            ref = (await gh.get(f"/git/ref/heads/{GH_BRANCH}")).raise_for_status().json()
            base_commit_sha = ref["object"]["sha"]
            base_commit = (await gh.get(f"/git/commits/{base_commit_sha}")).raise_for_status().json()
            base_tree_sha = base_commit["tree"]["sha"]
            tree_items = []
            for fn, e in edits.items():
                blob = (await gh.post("/git/blobs", json={"content": e["content"], "encoding": "utf-8"})).raise_for_status().json()
                tree_items.append({"path": fn, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            tree = (await gh.post("/git/trees", json={"base_tree": base_tree_sha, "tree": tree_items})).raise_for_status().json()
            commit = (await gh.post("/git/commits", json={"message": message, "tree": tree["sha"], "parents": [base_commit_sha]})).raise_for_status().json()
            (await gh.patch(f"/git/refs/heads/{GH_BRANCH}", json={"sha": commit["sha"]})).raise_for_status()
            return True, commit["sha"][:7]
    except httpx.HTTPStatusError as e:
        return False, f"GitHub API {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return False, str(e)


def _diff(edits: dict) -> str:
    out = []
    for fn, e in edits.items():
        out += list(difflib.unified_diff(
            e["original"].splitlines(keepends=True), e["content"].splitlines(keepends=True),
            fromfile=fn, tofile=fn, n=2))
    return "".join(out)


async def try_code_command(note: str, from_owner: bool, raw: str) -> str | None:
    """Only fires when the email carries the secret (trigger + auth). `raw` = full body."""
    if not has_secret(raw) and not has_secret(note):
        return None
    if not from_owner:
        return "That email carried the code passphrase but isn't from a recognised owner — ignored."

    request = (note or "").replace(CODE_SECRET, "").strip() or "(no instruction found)"
    logger.info("crm_coder: code-change request received")

    messages = [{"role": "user", "content": f"Owner's code-change request:\n{request}"}]
    edits: dict = {}
    final_text = ""
    try:
        for _ in range(MAX_STEPS):
            resp = await claude.messages.create(
                model=MODEL, max_tokens=8000, system=SYSTEM, tools=TOOLS, messages=messages,
            )
            if resp.stop_reason != "tool_use":
                final_text = next((b.text for b in resp.content if b.type == "text"), "")
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    out, err = _run_tool(b.name, b.input, edits)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:20000], "is_error": err})
            messages.append({"role": "user", "content": results})
    except Exception as e:
        logger.error(f"crm_coder: agent loop failed: {e}")
        _restore_originals(edits)
        return f"Hit an error making that change; reverted, nothing deployed. ({e})"

    if not edits:
        return f"I didn't change any code.\n\n{final_text}"

    changed = list(edits)
    ok, why = _tests_pass(changed)
    if not ok:
        _restore_originals(edits)
        return f"Made the change but it failed tests, so I reverted it — nothing deployed.\n\n{why}"

    diff = _diff(edits)
    pushed, info = await _commit_via_github(edits, f"CRM change via email: {request[:100]}\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>")
    _restore_originals(edits)  # container fs is ephemeral; source of truth is the pushed commit
    if not pushed:
        return f"Change passed tests but I couldn't ship it — nothing deployed.\n\n{info}"

    return (
        f"Done — shipped commit {info}, Railway is redeploying. Files: {', '.join(changed)}\n\n"
        f"{final_text}\n\n<b>Diff</b>\n<pre>{diff[:6000]}</pre>"
    )
