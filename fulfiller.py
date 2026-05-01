"""
Fulfiller loop — polls the AgentRegistry for pending queries targeting punk #2767,
calls the local /query endpoint, hashes the result, and submits fulfill() on-chain.
"""
import asyncio
import hashlib
import logging
import os

import httpx
from eth_account import Account

import store

logger = logging.getLogger(__name__)

PUNK_ID   = 2767
REGISTRY  = "0xbbf7a43ae525a0ea9b8210bcae664bb4c4848f60"
RPC_URL   = os.getenv("RPC_URL", "https://lite.chain.opentensor.ai")
CHAIN_ID  = 964
GAS_LIMIT = 250_000
POLL_INTERVAL = 10  # seconds


# ── JSON-RPC helpers ──────────────────────────────────────────────────────────

async def _rpc(client: httpx.AsyncClient, method: str, params: list):
    r = await client.post(RPC_URL, json={
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1,
    })
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data["result"]


def _enc(v: int) -> str:
    return hex(v)[2:].zfill(64)


async def _next_query_id(client: httpx.AsyncClient) -> int:
    result = await _rpc(client, "eth_call", [{"to": REGISTRY, "data": "0xc4c4829c"}, "latest"])
    return int(result, 16)


async def _get_query(client: httpx.AsyncClient, qid: int) -> dict:
    data = "0xab9c67ad" + _enc(qid)
    raw = (await _rpc(client, "eth_call", [{"to": REGISTRY, "data": data}, "latest"]))[2:]
    return {
        "punk_id":  int(raw[0:64],   16),
        "status":   int(raw[192:256], 16),  # 0=Pending 1=Fulfilled 2=Refunded 3=Expired
    }


# ── On-chain fulfill ──────────────────────────────────────────────────────────

async def _fulfill_onchain(client: httpx.AsyncClient, account, qid: int, result_hash: str) -> str:
    nonce     = int(await _rpc(client, "eth_getTransactionCount", [account.address, "pending"]), 16)
    gas_price = int(await _rpc(client, "eth_gasPrice", []), 16)

    calldata = "0x944b8aa2" + _enc(qid) + result_hash.replace("0x", "").zfill(64)

    tx = {
        "nonce":    nonce,
        "gasPrice": gas_price,
        "gas":      GAS_LIMIT,
        "to":       REGISTRY,
        "value":    0,
        "data":     calldata,
        "chainId":  CHAIN_ID,
    }
    signed  = account.sign_transaction(tx)
    tx_hash = await _rpc(client, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])

    # Wait up to 60s for receipt
    for _ in range(30):
        await asyncio.sleep(2)
        receipt = await _rpc(client, "eth_getTransactionReceipt", [tx_hash])
        if receipt:
            status = int(receipt.get("status", "0x0"), 16)
            if status == 1:
                logger.info(f"Fulfiller: query #{qid} fulfilled — {tx_hash}")
            else:
                logger.warning(f"Fulfiller: TX reverted for query #{qid} — {tx_hash}")
            return tx_hash
    logger.warning(f"Fulfiller: receipt timeout for query #{qid}")
    return tx_hash


# ── Process one query ─────────────────────────────────────────────────────────

async def _process(client: httpx.AsyncClient, account, qid: int):
    input_text = store.get_relay_input(qid)
    if not input_text:
        logger.debug(f"Fulfiller: query #{qid} waiting for relay input")
        return

    logger.info(f"Fulfiller: processing query #{qid}: {input_text[:80]}")

    port = int(os.getenv("PORT", 8000))
    try:
        r = await client.post(
            f"http://localhost:{port}/query",
            json={"query": input_text, "queryId": str(qid)},
            timeout=60,
        )
        response = r.json()["response"]
    except Exception as e:
        logger.error(f"Fulfiller: AI call failed for query #{qid}: {e}")
        return

    store.set_relay_result(qid, response)

    result_hash = "0x" + hashlib.sha256(response.encode()).hexdigest()
    await _fulfill_onchain(client, account, qid, result_hash)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_loop():
    key = os.getenv("FULFILLER_KEY")
    if not key:
        logger.warning("Fulfiller: FULFILLER_KEY not set — loop not starting")
        return

    account = Account.from_key(key)
    logger.info(f"Fulfiller: wallet={account.address}  punk=#{PUNK_ID}")

    # Give the API server time to start before we need it
    await asyncio.sleep(5)

    async with httpx.AsyncClient(timeout=30, base_url=RPC_URL) as client:
        # Start from current tip — don't try to process historical queries
        try:
            high_water = await _next_query_id(client)
            logger.info(f"Fulfiller: starting at query #{high_water}")
        except Exception as e:
            logger.error(f"Fulfiller: could not fetch initial nextQueryId: {e}")
            high_water = 1

        while True:
            try:
                next_id = await _next_query_id(client)
                for qid in range(high_water, next_id):
                    try:
                        q = await _get_query(client, qid)
                        if q["punk_id"] == PUNK_ID and q["status"] == 0:
                            await _process(client, account, qid)
                    except Exception as e:
                        logger.error(f"Fulfiller: error on query #{qid}: {e}")
                high_water = next_id
            except Exception as e:
                logger.error(f"Fulfiller: poll error: {e}")

            await asyncio.sleep(POLL_INTERVAL)
