import hashlib
import os
import httpx
from web3 import Web3
from dotenv import load_dotenv
load_dotenv()

RPC = "https://lite.chain.opentensor.ai"
REGISTRY = Web3.to_checksum_address("0xbbf7a43ae525a0ea9b8210bcae664bb4c4848f60")
PUNK_ID = 2767
QUESTION = "What is Bittensor?"
RELAY_URL = "https://axiom-bittensor-agent-production.up.railway.app/relay"

PRIVATE_KEY = os.getenv("OWNER_KEY", "0xYourPunkOwnerPrivateKey")

w3 = Web3(Web3.HTTPProvider(RPC))
account = w3.eth.account.from_key(PRIVATE_KEY)

registry = w3.eth.contract(address=REGISTRY, abi=[{
    "name": "query",
    "type": "function",
    "inputs": [
        {"name": "punkId", "type": "uint256"},
        {"name": "inputHash", "type": "bytes32"}
    ],
    "outputs": [{"name": "queryId", "type": "uint256"}],
    "stateMutability": "payable"
}])

input_hash = hashlib.sha256(QUESTION.encode()).digest()

tx = registry.functions.query(PUNK_ID, input_hash).build_transaction({
    "from": account.address,
    "value": w3.to_wei(0.0001, "ether"),
    "nonce": w3.eth.get_transaction_count(account.address),
    "chainId": 964,
    "gas": 200000,
})
signed = account.sign_transaction(tx)
tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
print(f"TX sent: {tx_hash.hex()}")

receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
print(f"TX confirmed, status: {receipt['status']}")

query_id = int(receipt["logs"][0]["topics"][1].hex(), 16)
print(f"Query ID: {query_id}")

httpx.post(RELAY_URL, json={"queryId": query_id, "input": QUESTION})
print(f"Relay posted. Check result at: {RELAY_URL.replace('/relay', '/relay/' + str(query_id))}")
