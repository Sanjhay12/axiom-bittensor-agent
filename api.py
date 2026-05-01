import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from anthropic import AsyncAnthropic
from chain import ChainReader, gather_chain_context

logger = logging.getLogger(__name__)

_claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_reader = ChainReader()


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_reader.prewarm())
    yield


app = FastAPI(lifespan=lifespan)

# Imported lazily to avoid circular init — agent.py is already loaded by the time
# this module is imported inside main()
def _get_constants():
    from agent import SYSTEM_PROMPT, DISCLAIMER, _INTENT_PROMPT
    return SYSTEM_PROMPT, DISCLAIMER, _INTENT_PROMPT


async def _get_fetch_plan(message: str) -> dict | None:
    _, _, _INTENT_PROMPT = _get_constants()
    try:
        result = await _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": _INTENT_PROMPT.format(
                history="none",
                message=message,
            )}],
        )
        text = result.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception:
        return None


class QueryIn(BaseModel):
    query: str
    queryId: str | None = None
    caller: str | None = None


@app.post("/query")
async def query_endpoint(req: QueryIn):
    SYSTEM_PROMPT, DISCLAIMER, _ = _get_constants()
    plan = await _get_fetch_plan(req.query)
    chain_context = await gather_chain_context(req.query, _reader, recent_history=[], plan=plan)
    system = SYSTEM_PROMPT
    if chain_context:
        system += f"\n\n---\n## Live On-Chain Data (use this for all numerical claims)\n{chain_context}\n---"
    result = await _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": req.query}],
    )
    response = result.content[0].text
    return {"response": response + DISCLAIMER, "queryId": req.queryId}


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "Axiom / TaoPunk #2767"}


@app.get("/")
async def root():
    return {
        "agent": "Axiom",
        "punk": "#2767",
        "protocol": "ERC-8041",
        "query_endpoint": "POST /query",
        "body": {"query": "<string>"},
    }
