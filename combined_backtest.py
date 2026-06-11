import json, math, random
import importlib.util

spec = importlib.util.spec_from_file_location("bt", "dfc_ma_backtest.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


def run_combined(vals, tactical_alloc=0.5, core_alloc=0.5,
                  short_p=bt.SHORT_MA, long_p_tac=bt.LONG_MA, long_p_core=bt.LONG_MA,
                  buy_amt=bt.BUY_AMT, sell_frac=bt.SELL_FRAC):
    tactical = bt.run_tactical_backtest(
        vals, short_p=short_p, long_p=long_p_tac,
        buy_amt=buy_amt * tactical_alloc, sell_frac=sell_frac,
    )
    core = bt.run_core_backtest(
        vals, long_p=long_p_core, buy_amt=buy_amt * core_alloc,
    )
    invested = tactical["invested"] + core["invested"]
    net = tactical["net"] + core["net"]
    return {
        "tactical": tactical,
        "core": core,
        "invested": invested,
        "net": net,
        "pct_of_invested": (net / invested * 100) if invested else 0,
    }


def report(name, vals, **kwargs):
    r = run_combined(vals, **kwargs)
    print(f"{name}: invested=${r['invested']:.2f} | net=${r['net']:.2f} "
          f"({r['pct_of_invested']:+.1f}%)  "
          f"[tactical {r['tactical']['pct_of_invested']:+.1f}%, "
          f"core {r['core']['pct_of_invested']:+.1f}%]")


if __name__ == "__main__":
    with open("tao_usd_history.json") as f:
        prices = json.load(f)
    downtrend_year = [p[1] for p in prices]

    # mild uptrend/chop sub-window from the same dataset
    mild_uptrend = downtrend_year[180:300]

    # synthetic flat/choppy series (same as whipsaw_test.py)
    random.seed(42)
    n = 400
    base = 200
    choppy = []
    for i in range(n):
        cycle = base * 0.08 * math.sin(2 * math.pi * i / 30)
        noise = random.gauss(0, base * 0.01)
        choppy.append(base + cycle + noise)

    splits = [
        ("50/50", 0.5, 0.5),
        ("70 core / 30 tactical", 0.3, 0.7),
        ("30 core / 70 tactical", 0.7, 0.3),
    ]

    for label, t_alloc, c_alloc in splits:
        print(f"\n=== Split: {label} ===")
        report("Downtrend year   ", downtrend_year, tactical_alloc=t_alloc, core_alloc=c_alloc,
               long_p_tac=100, long_p_core=100)
        report("Mild uptrend/chop", mild_uptrend, tactical_alloc=t_alloc, core_alloc=c_alloc,
               short_p=10, long_p_tac=30, long_p_core=50)
        report("Synthetic choppy ", choppy, tactical_alloc=t_alloc, core_alloc=c_alloc,
               short_p=10, long_p_tac=50, long_p_core=100)
