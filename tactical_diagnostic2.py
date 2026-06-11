import json
import importlib.util

spec = importlib.util.spec_from_file_location("bt", "dfc_ma_backtest.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

with open("tao_usdt_history.json") as f:
    series = json.load(f)
vals = [s["close"] for s in series]
dates = [s["date"] for s in series]
n = len(vals)

short_p, long_p = 10, 50
buy_amt, sell_frac = bt.BUY_AMT, bt.SELL_FRAC

cash = 0.0
tao = 0.0
cost_basis = 0.0

# track position size + cost basis at ~90 day intervals
checkpoints = []

for i in range(long_p, n):
    price = vals[i]
    m_short = bt.ma(vals, short_p, i)
    m_long = bt.ma(vals, long_p, i)
    m_long_prev = bt.ma(vals, long_p, i - 1)
    long_rising = m_long > m_long_prev

    if price < m_short and (price > m_long or long_rising):
        new_tao = buy_amt / price
        cost_basis = (cost_basis * tao + price * new_tao) / (tao + new_tao)
        tao += new_tao
    elif price > m_short:
        sell_amt = tao * sell_frac
        tao -= sell_amt

    if (i - long_p) % 90 == 0:
        checkpoints.append((dates[i], price, tao, tao * price, cost_basis))

print(f"{'date':>12} | {'price':>8} | {'tao held':>9} | {'position $':>10} | {'cost basis':>10} | {'unrealized %'}")
for d, price, t, val, cb in checkpoints:
    unreal = (price / cb - 1) * 100 if cb else 0
    print(f"{d:>12} | ${price:>7.2f} | {t:>9.4f} | ${val:>9.2f} | ${cb:>9.2f} | {unreal:+.1f}%")

# final
d, price = dates[-1], vals[-1]
unreal = (price / cost_basis - 1) * 100 if cost_basis else 0
print(f"{d:>12} | ${price:>7.2f} | {tao:>9.4f} | ${tao*price:>9.2f} | ${cost_basis:>9.2f} | {unreal:+.1f}%")
