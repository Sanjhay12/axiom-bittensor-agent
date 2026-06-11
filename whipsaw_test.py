import json, math, random
import importlib.util

spec = importlib.util.spec_from_file_location("bt", "dfc_ma_backtest.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

random.seed(42)

# synthetic flat/choppy series: oscillate around $200 with a ~30-day cycle, +/-8%, plus daily noise
n = 400
base = 200
vals = []
for i in range(n):
    cycle = base * 0.08 * math.sin(2 * math.pi * i / 30)
    noise = random.gauss(0, base * 0.01)
    vals.append(base + cycle + noise)

print(f"synthetic series: start={vals[0]:.1f} end={vals[-1]:.1f} "
      f"({(vals[-1]/vals[0]-1)*100:+.1f}% buy-and-hold), "
      f"min={min(vals):.1f} max={max(vals):.1f}")

print("--- Tactical bucket ---")
for short_p, long_p in [(20, 100), (10, 50)]:
    r = bt.run_tactical_backtest(vals, short_p=short_p, long_p=long_p)
    print(f"short={short_p:>2} long={long_p:>3} | days={r['days']:>3} | "
          f"buys={r['buy_days']:>3} | sells={r['sell_days']:>3} | "
          f"invested=${r['invested']:.0f} | net=${r['net']:.2f} "
          f"({r['pct_of_invested']:+.1f}%)")

print("--- Core bucket ---")
for long_p in [30, 50, 100]:
    r = bt.run_core_backtest(vals, long_p=long_p)
    print(f"long={long_p:>3} | days={r['days']:>3} | entries={r['entries']:>2} | "
          f"exits={r['exits']:>2} | invested=${r['invested']:.0f} | "
          f"net=${r['net']:.2f} ({r['pct_of_invested']:+.1f}%)")
