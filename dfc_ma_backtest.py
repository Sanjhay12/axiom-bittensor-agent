import json
with open("tao_usd_history.json") as f:
    prices = json.load(f)


vals =[p[1] for p in prices]
n = len(vals)

SHORT_MA = 30
LONG_MA = 50
BUY_AMT = 1
SELL_FRAC = 0.1
FEE = 0.001  # fraction taken on each buy/sell (0.1% per side)

def ma(vals, period, i):
    return sum(vals[i-period:i])/period 


def run_tactical_backtest(vals, short_p = SHORT_MA, long_p = LONG_MA, buy_amt = BUY_AMT, sell_frac = SELL_FRAC, min_profit = 0.0, fee = FEE):
    n = len(vals)
    cash = 0
    tao = 0
    invested = 0
    realized = 0.0
    buy_days = 0
    sell_days = 0
    cost_basis = 0.0  # weighted-average price paid for currently-held tao


    for i in range(long_p, n):
        price = vals[i]
        m_short = ma(vals, short_p, i)
        m_long = ma(vals, long_p, i)
        m_long_prev = ma(vals, long_p, i-1)
        long_rising = m_long > m_long_prev

        if price < m_short and (price > m_long or long_rising):
              new_tao = (buy_amt * (1 - fee)) / price
              if tao + new_tao > 0:
                  cost_basis = (cost_basis * tao + price * new_tao) / (tao + new_tao)
              tao += new_tao
              cash -= buy_amt
              invested += buy_amt
              buy_days += 1
        # cost-basis-gated sell (commented out — caused the strategy to hold an ever-growing
        # underwater bag instead of cycling cash, making losses worse, not better):
        # elif price > m_short and tao > 0 and price > cost_basis * (1 + min_profit):
        elif price > m_short:
              sell_amt = tao * sell_frac
              tao -= sell_amt
              cash += sell_amt * price * (1 - fee)
              realized += sell_amt * price * (1 - fee)
              sell_days += 1

    final_value = cash + tao * vals[-1]
    return {
        "days": n - long_p,
        "buy_days": buy_days,
        "sell_days": sell_days,
        "invested": invested,
        "remaining_tao": tao,
        "remaining_value": tao * vals[-1],
        "cost_basis": cost_basis,
        "realized": realized,
        "net": final_value,
        "pct_of_invested": (final_value / invested * 100) if invested else 0,
    }


def run_core_backtest(vals, long_p = LONG_MA, buy_amt = 1.0, fee = FEE):
    n = len(vals)
    cash = 0.0
    tao = 0.0
    invested = 0.0
    realized = 0.0
    in_position = False
    entries = 0
    exits = 0

    for i in range(long_p, n):
        price = vals[i]
        m_long = ma(vals, long_p, i)
        m_long_prev = ma(vals, long_p, i-1)
        long_rising = m_long > m_long_prev
        uptrend = price > m_long or long_rising 
        
        if uptrend and not in_position:
              # go all-in with this bucket's allocation
              tao += (buy_amt * (1 - fee)) / price
              cash -= buy_amt
              invested += buy_amt
              in_position = True
              entries += 1
        elif not uptrend and in_position:
              # exit fully to cash
              sell_value = tao * price * (1 - fee)
              cash += sell_value
              realized += sell_value
              tao = 0.0
              in_position = False
              exits += 1

    final_value = cash + tao * vals[-1]
    return {
        "days": n - long_p,
        "entries": entries,
        "exits": exits,
        "invested": invested,
        "remaining_tao": tao,
        "remaining_value": tao * vals[-1],
        "realized": realized,
        "net": final_value,
        "pct_of_invested": (final_value / invested * 100) if invested else 0,
    }


if __name__ == "__main__":
    print("--- Tactical bucket ---")
    for long_p in [50, 100, 150]:
        r = run_tactical_backtest(vals, long_p=long_p)
        print(
            f"long_p={long_p:>3} | days={r['days']:>3} | buys={r['buy_days']:>3} | "
            f"sells={r['sell_days']:>3} | invested=${r['invested']:.0f} | "
            f"remaining={r['remaining_tao']:.3f} TAO (${r['remaining_value']:.2f}) | "
            f"realized=${r['realized']:.2f} | net=${r['net']:.2f} "
            f"({r['pct_of_invested']:+.1f}%)"
        )

    print("\n--- Core bucket ---")
    for long_p in [50, 100, 150]:
        r = run_core_backtest(vals, long_p=long_p)
        print(
            f"long_p={long_p:>3} | days={r['days']:>3} | entries={r['entries']:>2} | "
            f"exits={r['exits']:>2} | invested=${r['invested']:.0f} | "
            f"remaining={r['remaining_tao']:.3f} TAO (${r['remaining_value']:.2f}) | "
            f"realized=${r['realized']:.2f} | net=${r['net']:.2f} "
            f"({r['pct_of_invested']:+.1f}%)"
        )