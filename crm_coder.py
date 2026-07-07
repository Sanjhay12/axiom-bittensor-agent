"""
Level 3 — the owner changes the CRM's CODE by email (direct-change mode).

An owner emails a code-change request that INCLUDES the shared secret; a Claude
coding loop (Opus 4.8, tool-use) edits the crm_* source in the repo, the change is
test-gated, and only on green is it committed, pushed (Railway auto-deploys), and the
diff emailed back. Per the owner's choice this is direct (no PR).

Guardrails (see crm_agent wiring):
  1. Shared secret in the email body — the real auth. `From:` is spoofable, so a code
     change only runs when CRM_CODE_SECRET is present. Its presence is also the trigger,
     so ordinary emails can never touch code.
  2. Tests must pass (py_compile of changed files + test_crm.py) before anything is pushed;
     a failing change is reverted, never deployed.
  3. The exact git diff is emailed back to whoever asked — an audit trail.

Only crm_* source files are editable — never .env, trading code, infra, or this file.

Deploy requirements (Railway env): CRM_CODE_SECRET (the passphrase) and GITHUB_TOKEN
(a PAT with push access, used to push from the container).
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import subprocess

from anthropic import AsyncAnthropic

import crm_mail

logger = logging.getLogger(__name__)

claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = "claude-opus-4-8"

REPO = os.path.dirname(os.path.abspath(__file__))
CODE_SECRET = os.getenv("CRM_CODE_SECRET")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIT_REMOTE = "https://github.com/Sanjhay12/axiom-bittensor-agent.git"

# Editable surface: crm_* modules only. Never .env, store/trader/risk (trading), this file.
_EDITABLE_RE = re.compile(r"^crm_[a-z0-9_]+\.py$")
_PROTECTED = {"crm_coder.py", "crm_mail.py"}  # coder can't rewrite its own auth path
MAX_STEPS = 24

SYSTEM = """You are a careful software engineer editing a Python fundraising-CRM codebase from an emailed request by its owner. Make the smallest change that fully satisfies the request.

Rules:
- Only edit crm_*.py files (never .env, trading code, or infra). Use list_files to see what's editable.
- read_file before write_file. write_file replaces the whole file, so read it, change what's needed, and write the complete new contents.
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


def _safe_path(path: str) -> str | None:
    name = os.path.basename((path or "").strip())
    if name != (path or "").strip().lstrip("./"):
        return None
    if not _EDITABLE_RE.match(name) or name in _PROTECTED:
        return None
    return os.path.join(REPO, name)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)


def _run_tool(name: str, inp: dict, written: set) -> tuple[str, bool]:
    """Returns (result_text, is_error)."""
    if name == "list_files":
        files = sorted(f for f in os.listdir(REPO) if _EDITABLE_RE.match(f) and f not in _PROTECTED)
        return "\n".join(files), False
    if name == "read_file":
        p = _safe_path(inp.get("path", ""))
        if not p or not os.path.exists(p):
            return f"Cannot read {inp.get('path')!r}: not an editable crm_* file.", True
        with open(p, encoding="utf-8") as f:
            return f.read(), False
    if name == "write_file":
        p = _safe_path(inp.get("path", ""))
        if not p:
            return f"Refused: {inp.get('path')!r} is not an editable crm_* file.", True
        with open(p, "w", encoding="utf-8") as f:
            f.write(inp.get("content", ""))
        written.add(os.path.basename(p))
        return f"Wrote {os.path.basename(p)}.", False
    return f"Unknown tool {name}", True


def _tests_pass(changed: set) -> tuple[bool, str]:
    """Rail 2: compile every changed file; run test_crm.py if present. No push on failure."""
    for f in changed:
        r = subprocess.run(["python", "-m", "py_compile", os.path.join(REPO, f)], capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"{f} does not compile:\n{r.stderr[-800:]}"
    if os.path.exists(os.path.join(REPO, "test_crm.py")):
        r = subprocess.run(["python", "-m", "pytest", "-q", "test_crm.py"], cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"test_crm.py failed:\n{(r.stdout + r.stderr)[-1200:]}"
    return True, ""


def _revert(changed: set):
    for f in changed:
        _git("checkout", "--", f)


def _commit_and_push(changed: set, request: str, requester: str) -> tuple[bool, str]:
    _git("add", *changed)
    msg = f"CRM change via email: {request[:120]}\n\nRequested by {requester}\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
    c = _git("commit", "-m", msg)
    if c.returncode != 0:
        return False, f"commit failed: {c.stderr[-400:]}"
    if not GITHUB_TOKEN:
        return False, "committed locally but GITHUB_TOKEN is not set, so I couldn't push (change will be lost on redeploy). Set GITHUB_TOKEN on the CRM service."
    push_url = GIT_REMOTE.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@")
    p = _git("push", push_url, "HEAD:main")
    if p.returncode != 0:
        return False, f"push failed: {p.stderr[-400:]}"
    return True, ""


async def try_code_command(note: str, from_owner: bool, raw: str) -> str | None:
    """Only fires when the email carries the secret (trigger + auth). `raw` is the full body."""
    if not has_secret(raw) and not has_secret(note):
        return None  # not a code request — fall through to config/directives/answer
    if not from_owner:
        return "That email carried the code passphrase but isn't from a recognised owner — ignored."

    request = (note or "").replace(CODE_SECRET, "").strip() or "(no instruction found)"
    logger.info("crm_coder: code-change request received")

    messages = [{"role": "user", "content": f"Owner's code-change request:\n{request}"}]
    written: set = set()
    final_text = ""
    try:
        for _ in range(MAX_STEPS):
            resp = await claude.messages.create(
                model=MODEL, max_tokens=8000,
                system=SYSTEM, tools=TOOLS, messages=messages,
            )
            if resp.stop_reason != "tool_use":
                final_text = next((b.text for b in resp.content if b.type == "text"), "")
                break
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    out, err = _run_tool(b.name, b.input, written)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:20000], "is_error": err})
            messages.append({"role": "user", "content": results})
    except Exception as e:
        logger.error(f"crm_coder: agent loop failed: {e}")
        _revert(written)
        return f"Hit an error making that change; reverted, nothing deployed. ({e})"

    if not written:
        return f"I didn't change any code.\n\n{final_text}"

    ok, why = _tests_pass(written)
    if not ok:
        _revert(written)
        return f"Made the change but it failed tests, so I reverted it — nothing deployed.\n\n{why}"

    diff = _git("diff", "--cached", "--stat").stdout or ""
    full_diff = _git("diff", "HEAD", "--", *written).stdout
    pushed, err = _commit_and_push(written, request, "owner")
    if not pushed:
        _revert(written)
        return f"Change passed tests but I couldn't ship it — reverted.\n\n{err}"

    return (
        f"Done — shipped and deploying. Files: {', '.join(sorted(written))}\n\n"
        f"{final_text}\n\n<b>Diff</b>\n<pre>{(full_diff or diff)[:6000]}</pre>"
    )
