import json
import importlib.util

spec = importlib.util.spec_from_file_location("bt", "dfc_ma_backtest.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

with open("tao_usdt_history.json") as f:
    series = json.load(f)

vals = [s["close"] for s in series]
print(f"{len(vals)} days, {series[0]['date']} to {series[-1]['date']}")
print(f"buy-and-hold: {(vals[-1]/vals[0]-1)*100:+.1f}%\n")

for long_p in [50, 100, 150]:
    print(f"=== core, long_p={long_p} ===")
    for margin in [0.0, 0.01, 0.02, 0.03, 0.05]:
        r = bt.run_core_backtest(vals, long_p=long_p, margin=margin)
        print(f"margin={margin:.2f}: entries={r['entries']:>2} | exits={r['exits']:>2} | "
              f"invested=${r['invested']:.2f} | net=${r['net']:.2f} ({r['pct_of_invested']:+.1f}%)")
    print()
