"""Run once to dump all SubtensorModule storage key names from the live chain."""
import bittensor as bt

sub = bt.subtensor(network="finney")
meta = sub.substrate.get_metadata()

pallets = meta.value[1]["V14"]["pallets"]
for p in pallets:
    if p.get("name") == "SubtensorModule":
        entries = p.get("storage", {}).get("entries", [])
        print(f"Found {len(entries)} storage entries:")
        for e in sorted(entries, key=lambda x: x["name"]):
            print(f"  {e['name']}")
        break
