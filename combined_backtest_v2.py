import json
import importlib.util

spec = importlib.util.spec_from_file_location("cb", "combined_backtest.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)

with open("tao_usdt_history.json") as f:
    series = json.load(f)

vals = [s["close"] for s in series]
print(f"{len(vals)} days, {series[0]['date']} to {series[-1]['date']}")
print(f"buy-and-hold: {(vals[-1]/vals[0]-1)*100:+.1f}%\n")

splits = [
    ("50/50", 0.5, 0.5),
    ("70 core / 30 tactical", 0.3, 0.7),
    ("30 core / 70 tactical", 0.7, 0.3),
    ("core only", 0.0, 1.0),
    ("tactical only", 1.0, 0.0),
]

ma_combos = [
    (10, 50, 50),
    (20, 100, 100),
    (10, 50, 100),
]

for short_p, long_p_tac, long_p_core in ma_combos:
    print(f"=== MA combo: short={short_p} long_tac={long_p_tac} long_core={long_p_core} ===")
    for label, t_alloc, c_alloc in splits:
        cb.report(
            label, vals,
            tactical_alloc=t_alloc, core_alloc=c_alloc,
            short_p=short_p, long_p_tac=long_p_tac, long_p_core=long_p_core,
        )
    print()
