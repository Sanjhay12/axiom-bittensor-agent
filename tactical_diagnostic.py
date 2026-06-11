import json
import importlib.util

spec = importlib.util.spec_from_file_location("bt", "dfc_ma_backtest.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

with open("tao_usdt_history.json") as f:
    series = json.load(f)
vals = [s["close"] for s in series]
n = len(vals)

short_p, long_p = 10, 50
buy_amt, sell_frac = bt.BUY_AMT, bt.SELL_FRAC

cash = 0.0
tao = 0.0
buy_prices = []
sell_prices = []

for i in range(long_p, n):
    price = vals[i]
    m_short = bt.ma(vals, short_p, i)
    m_long = bt.ma(vals, long_p, i)
    m_long_prev = bt.ma(vals, long_p, i - 1)
    long_rising = m_long > m_long_prev

    if price < m_short and (price > m_long or long_rising):
        tao += buy_amt / price
        cash -= buy_amt
        buy_prices.append(price)
    elif price > m_short:
        sell_amt = tao * sell_frac
        tao -= sell_amt
        cash += sell_amt * price
        sell_prices.append(price)

avg_buy = sum(buy_prices) / len(buy_prices)
avg_sell = sum(sell_prices) / len(sell_prices)

print(f"buys: {len(buy_prices)}  avg buy price: ${avg_buy:.2f}")
print(f"sells: {len(sell_prices)}  avg sell price: ${avg_sell:.2f}")
print(f"avg sell vs avg buy: {(avg_sell/avg_buy - 1)*100:+.1f}%")
print(f"final tao held: {tao:.4f} (worth ${tao*vals[-1]:.2f} at ${vals[-1]:.2f})")
print(f"cash flow: ${cash:.2f}")
print(f"final price: ${vals[-1]:.2f}")
print(f"net: ${cash + tao*vals[-1]:.2f}")

tao_bought = sum(buy_amt / p for p in buy_prices)
tao_sold = tao_bought - tao
print(f"\ntao bought total: {tao_bought:.4f}")
print(f"tao sold total:   {tao_sold:.4f}")
print(f"tao remaining:    {tao:.4f} ({tao/tao_bought*100:.1f}% of what was bought, still unsold)")
