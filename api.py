import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


class DevQueryIn(BaseModel):
    query: str
    password: str


@app.post("/dev/query")
async def dev_query(req: DevQueryIn):
    dev_password = os.getenv("DEV_PASSWORD", "")
    if not dev_password or req.password != dev_password:
        raise HTTPException(status_code=401, detail="Invalid dev password")
    SYSTEM_PROMPT, DISCLAIMER, _ = _get_constants()
    plan = await _get_fetch_plan(req.query)
    chain_context = await gather_chain_context(req.query, _reader, recent_history=[], plan=plan)
    system = SYSTEM_PROMPT
    if chain_context:
        system += f"\n\n---\n## Live On-Chain Data\n{chain_context}\n---"
    result = await _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system,
        messages=[{"role": "user", "content": req.query}],
    )
    return {"response": result.content[0].text + DISCLAIMER}


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


RPC_URL          = os.getenv("RPC_URL", "https://lite.chain.opentensor.ai")
FULL_FEE_WEI     = 0  # testing: free
HALF_FEE_WEI     = 0  # testing: free
SUB_DURATION_SEC = 30 * 24 * 3600       # 30 days


async def _verify_sub_tx(client: httpx.AsyncClient, tx_hash: str, from_wallet: str, to_wallet: str, min_wei: int = HALF_FEE_WEI) -> bool:
    receipt_r = await client.post(RPC_URL, json={
        "jsonrpc": "2.0", "method": "eth_getTransactionReceipt",
        "params": [tx_hash], "id": 1,
    })
    receipt = receipt_r.json().get("result")
    if not receipt or receipt.get("status") != "0x1":
        return False

    tx_r = await client.post(RPC_URL, json={
        "jsonrpc": "2.0", "method": "eth_getTransactionByHash",
        "params": [tx_hash], "id": 1,
    })
    tx = tx_r.json().get("result")
    if not tx:
        return False

    if tx.get("from", "").lower() != from_wallet.lower():
        return False
    if tx.get("to", "").lower() != to_wallet.lower():
        return False
    if int(tx.get("value", "0x0"), 16) < min_wei:
        return False
    return True


@app.get("/config")
async def config():
    return {
        "owner_wallet":   os.getenv("OWNER_WALLET", ""),
        "partner_wallet": os.getenv("PARTNER_WALLET", ""),
        "bot_username":   os.getenv("BOT_USERNAME", ""),
    }


class DevSubscribeIn(BaseModel):
    wallet:   str
    password: str


@app.post("/dev/subscribe")
async def dev_subscribe(req: DevSubscribeIn):
    dev_password = os.getenv("DEV_PASSWORD", "")
    if not dev_password or req.password != dev_password:
        raise HTTPException(status_code=401, detail="Invalid dev password")
    expires_at = int(time.time()) + SUB_DURATION_SEC
    store.upsert_subscription(req.wallet, "dev", expires_at)
    code = store.create_access_code(req.wallet)
    bot_username = os.getenv("BOT_USERNAME", "")
    telegram_link = f"https://t.me/{bot_username}?start={code}" if bot_username else None
    return {"ok": True, "expires_at": expires_at, "telegram_link": telegram_link}


class SubscribeIn(BaseModel):
    wallet:    str
    tx_hash_1: str
    tx_hash_2: str | None = None  # only required when PARTNER_WALLET is set


@app.post("/subscribe")
async def subscribe(req: SubscribeIn):
    owner   = os.getenv("OWNER_WALLET", "").lower()
    partner = os.getenv("PARTNER_WALLET", "").lower()
    if not owner:
        raise HTTPException(status_code=500, detail="OWNER_WALLET not configured")

    async with httpx.AsyncClient(timeout=15) as client:
        if partner:
            ok1, ok2 = await asyncio.gather(
                _verify_sub_tx(client, req.tx_hash_1, req.wallet, owner),
                _verify_sub_tx(client, req.tx_hash_2, req.wallet, partner),
            )
            if not ok1 or not ok2:
                raise HTTPException(status_code=400, detail="One or both transactions are invalid or insufficient")
        else:
            ok1 = await _verify_sub_tx(client, req.tx_hash_1, req.wallet, owner, min_wei=FULL_FEE_WEI)
            if not ok1:
                raise HTTPException(status_code=400, detail="Transaction not valid or insufficient payment")

    expires_at = int(time.time()) + SUB_DURATION_SEC
    store.upsert_subscription(req.wallet, req.tx_hash_1, expires_at)
    code = store.create_access_code(req.wallet)
    bot_username = os.getenv("BOT_USERNAME", "")
    telegram_link = f"https://t.me/{bot_username}?start={code}" if bot_username else None
    return {"ok": True, "expires_at": expires_at, "telegram_link": telegram_link}


@app.get("/subscribe/status/{wallet}")
async def subscribe_status(wallet: str):
    sub = store.get_subscription(wallet)
    if not sub or sub["expires_at"] <= int(time.time()):
        return {"active": False}
    return {"active": True, "expires_at": sub["expires_at"]}


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
