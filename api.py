import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from anthropic import AsyncAnthropic
from chain import ChainReader, gather_chain_context
import store

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


class RelayIn(BaseModel):
    queryId: int
    input: str


@app.post("/relay")
async def relay_in(req: RelayIn):
    store.insert_relay_query(req.queryId, req.input)
    return {"ok": True}


@app.get("/relay/{query_id}")
async def relay_out(query_id: int):
    row = store.get_relay_result(query_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Query not found")
    if row["result_text"] is None:
        return {"status": "pending", "queryId": query_id}
    return {"status": "fulfilled", "queryId": query_id, "result": row["result_text"]}


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "Axiom / TaoPunk #2767"}


@app.get("/")
async def root():
    return {
        "name": "Axiom",
        "description": "Bittensor research agent — live on-chain data, subnet analysis, validator intelligence.",
        "version": "1.0.0",
        "endpoint": "https://axiom-bittensor-agent-production.up.railway.app/query",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"}
            },
            "required": ["query"]
        }
    }
