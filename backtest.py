"""
Backtest the Axiom trading strategy against historical data in backtest_snapshots.

Run: python backtest.py
"""
import logging
import time
from collections import defaultdict
from datetime import datetime

import psycopg2
import psycopg2.extras
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import risk
import analytics
from dataclasses import dataclass
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

DB_URL = "postgresql://postgres:hIrjDWVQIQDuxduRhOXsoiYpcFTHlSke@switchyard.proxy.rlwy.net:32597/railway"


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DB_URL)


def rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def get_timestamps(conn) -> list[int]:
    result = rows(conn, "SELECT DISTINCT ts FROM backtest_snapshots ORDER BY ts ASC")
    return [r["ts"] for r in result]


def preload_all(conn) -> dict:
    """Load entire backtest_snapshots into memory keyed by netuid."""
    all_rows = rows(conn, "SELECT * FROM backtest_snapshots ORDER BY netuid, ts ASC")
    data = defaultdict(list)
    for r in all_rows:
        data[r["netuid"]].append(r)
    return data


def get_netuids_at_ts(cache: dict, ts: int) -> list[int]:
    return [n for n, rs in cache.items() if any(r["ts"] == ts for r in rs)]


def get_subnet_history_asof(cache: dict, netuid: int, as_of_ts: int, days: int = 30) -> list[dict]:
    since = as_of_ts - days * 86400
    return [r for r in cache.get(netuid, []) if since <= r["ts"] <= as_of_ts]


def get_alpha_price_history_asof(cache: dict, netuid: int, as_of_ts: int, days: int = 30) -> list[dict]:
    since = as_of_ts - days * 86400
    return [{"ts": r["ts"], "alpha_price_tao": r["alpha_price_tao"]}
            for r in cache.get(netuid, [])
            if since <= r["ts"] <= as_of_ts and r.get("alpha_price_tao") is not None]


def get_latest_snapshot_asof(cache: dict, netuid: int, as_of_ts: int) -> dict | None:
    candidates = [r for r in cache.get(netuid, []) if r["ts"] <= as_of_ts]
    return candidates[-1] if candidates else None


# ── Signal scoring (mirrors trader.py but uses backtest data) ─────────────────

def score_subnet_asof(cache: dict, netuid: int, as_of_ts: int, return_signals: bool = False):
    # Early exit if insufficient data
    history = get_subnet_history_asof(cache, netuid, as_of_ts, days=7)
    if len(history) < 4:
        return (0.0, 0.0, []) if return_signals else (0.0, 0.0)

    import store
    orig_subnet_history       = store.get_subnet_history
    orig_price_history        = store.get_alpha_price_history
    orig_validator_stakes     = store.get_latest_validator_stakes
    orig_churn_history        = store.get_churn_history

    store.get_subnet_history          = lambda n, days=30: get_subnet_history_asof(cache, n, as_of_ts, days)
    store.get_alpha_price_history     = lambda n, days=30: get_alpha_price_history_asof(cache, n, as_of_ts, days)
    store.get_latest_validator_stakes = lambda n: {}
    store.get_churn_history           = lambda n, days=7: []
    analytics.set_cache_ts(as_of_ts)

    try:
        signals = [
            analytics.emission_price_divergence(netuid),
            analytics.alpha_price_momentum(netuid),
            analytics.zscore_anomaly(netuid),
            analytics.regime_detection(netuid),
            # analytics.death_spiral_warning(netuid),      # no churn data in snapshots
            # analytics.stake_concentration_gini(netuid),  # no validator stake data in snapshots
            analytics.kalman_filter_price(netuid),
            analytics.registration_cost_velocity(netuid),
            # analytics.monte_carlo_emission(netuid),         # noise: skip in backtest
            # analytics.hmm_regime_detection(netuid),      # slow: skip in backtest
            #
            analytics.isolation_forest_anomaly(netuid),  # slow: skip in backtest
        ]
        total_weight = sum(s.confidence for s in signals if s.confidence > 0)
        if total_weight == 0:
            return (0.0, 0.0, signals) if return_signals else (0.0, 0.0)
        weighted_score = sum(s.score * s.confidence for s in signals if s.confidence > 0) / total_weight
        avg_confidence = total_weight / len(signals)
        if return_signals:
            return round(weighted_score, 2), round(avg_confidence, 2), signals
        return round(weighted_score, 2), round(avg_confidence, 2)
    finally:
        store.get_subnet_history          = orig_subnet_history
        store.get_alpha_price_history     = orig_price_history
        store.get_latest_validator_stakes = orig_validator_stakes
        store.get_churn_history           = orig_churn_history


# ── Portfolio state ───────────────────────────────────────────────────────────

@dataclass
class Position:
    netuid:        int
    entry_ts:      int
    entry_price:   float
    size_tao:      float
    peak_price:    float
    entry_signals: list = None


def check_entry(recent_scores: list[float], signals: list) -> bool:
    if len(recent_scores) < risk.ENTRY_CYCLES_REQUIRED:
        return False
    if not all(s>risk.ENTRY_SCORE_THRESHOLD for s in recent_scores[-risk.ENTRY_CYCLES_REQUIRED:]):
        return False
    if signals:
        strong_signals = sum(1 for s in signals if s.score>=5 and s.confidence > 0)
        #kalman = next((s for s in signals if s.model=="kalman_filter_price"), None)
        #if kalman and kalman.score < 7:
         #   return False
        
        if strong_signals < 2:
            return False
    return True
    
    return all(s > risk.ENTRY_SCORE_THRESHOLD for s in recent_scores[-risk.ENTRY_CYCLES_REQUIRED:])


def check_exit(pos: Position, current_price: float, current_score: float, ts, recent_exit_scores: list[float]) -> tuple[bool, str]:
    pnl_pct = (current_price - pos.entry_price) / pos.entry_price
    if pnl_pct <= -risk.STOP_LOSS:
        return True, "stop_loss"
    if pnl_pct >= risk.TAKE_PROFIT:
        return True, "take_profit"
    drawdown = (current_price - pos.peak_price) / pos.peak_price
    if drawdown <= -risk.TRAILING_STOP:
        return True, "trailing_stop"
    days_held = (ts-pos.entry_ts) / 86400
    if (len(recent_exit_scores ) >= risk.EXIT_CYCLES_REQUIRED and all(s<risk.EXIT_SCORE_THRESHOLD for s in recent_exit_scores[-risk.EXIT_CYCLES_REQUIRED:])):
        return True, "signal_exit"
    return False, ""


def max_drawdown(trades: list, starting_portfolio: float = 100.0) -> float:
    if not trades:
        return 0.0
    portfolio = starting_portfolio
    peak = starting_portfolio
    max_dd = 0.0
    for t in trades:
        portfolio += t["pnl_tao"]
        if portfolio > peak:
            peak = portfolio
        dd = (peak - portfolio) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 2)



# ── Main backtest loop ────────────────────────────────────────────────────────

def run_backtest():
    conn = get_conn()
    logger.info("Loading all backtest data into memory...")
    cache      = preload_all(conn)
    timestamps = get_timestamps(conn)
    conn.close()

    logger.info(f"Backtest: {len(timestamps)} timestamps, {len(cache)} subnets")

    portfolio     = risk.PORTFOLIO_SIZE_TAO
    positions     = {}
    closed_trades = []
    score_history = defaultdict(list)
    exit_score_history = defaultdict(list)
    cooldown_until = {}
    signal_stats  = defaultdict(lambda: {"fired": 0, "total": 0})
    equity_curve  = []

    for ts in tqdm(timestamps, desc="Backtesting", unit="day"):
        netuids      = get_netuids_at_ts(cache, ts)
        deployed_tao = sum(p.size_tao for p in positions.values())
        
        # ── Exit pass ────────────────────────────────────────────────────────
        for netuid in list(positions.keys()):
            pos  = positions[netuid]
            snap = get_latest_snapshot_asof(cache, netuid, ts)
            if not snap or not snap.get("alpha_price_tao"):
                continue
            current_price  = snap["alpha_price_tao"]
            pos.peak_price = max(pos.peak_price, current_price)

            score, _ = score_subnet_asof(cache, netuid, ts)
            exit_score_history[netuid].append(score)
            should_exit, reason = check_exit(pos, current_price, score, ts, exit_score_history[netuid])

            if should_exit:
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price
                pnl_tao = pnl_pct * pos.size_tao
                portfolio    += pos.size_tao + pnl_tao
                deployed_tao -= pos.size_tao
                closed_trades.append({
                    "netuid":        netuid,
                    "entry_ts":      pos.entry_ts,
                    "exit_ts":       ts,
                    "entry_price":   pos.entry_price,
                    "exit_price":    current_price,
                    "pnl_pct":       round(pnl_pct * 100, 2),
                    "pnl_tao":       round(pnl_tao, 4),
                    "reason":        reason,
                    "entry_signals": pos.entry_signals or [],
                })
                del positions[netuid]
                cooldown_until[netuid] = ts + risk.COOLDOWN_CYCLES * 86400
                logger.info(f"  EXIT SN{netuid} {reason} P&L {pnl_pct:+.1%}")

        # ── Entry pass ───────────────────────────────────────────────────────
        for netuid in netuids:
            
            if netuid in positions:
                continue
            if cooldown_until.get(netuid, 0) > ts:
                continue
            if len(positions) >= risk.MAX_OPEN_POSITIONS:
                break
            if deployed_tao / portfolio >= risk.MAX_TOTAL_DEPLOYED:
                break

            snap = get_latest_snapshot_asof(cache, netuid, ts)
            if not snap or not snap.get("alpha_price_tao"):
                continue

            score, confidence, signals = score_subnet_asof(cache, netuid, ts, return_signals=True)
            score_history[netuid].append(score)
            for s in signals:
                signal_stats[s.model]["total"] += 1
                if s.score > 0 and s.confidence > 0:
                    signal_stats[s.model]["fired"] += 1

            if not check_entry(score_history[netuid], signals):
                continue

            current_price = snap["alpha_price_tao"]
            size_tao = portfolio * (0.15 if score >= 8 else 0.08)
            size_tao = min(size_tao, portfolio * risk.MAX_POSITION_SIZE)

            if deployed_tao + size_tao > portfolio * risk.MAX_TOTAL_DEPLOYED:
                continue
            if portfolio - deployed_tao - size_tao < risk.MIN_TAO_BALANCE:
                continue

            portfolio    -= size_tao
            deployed_tao += size_tao
            positions[netuid] = Position(
                netuid=netuid, entry_ts=ts,
                entry_price=current_price, size_tao=size_tao,
                peak_price=current_price,
                entry_signals=signals
            )
            logger.info(f"  ENTRY SN{netuid} @ {current_price:.4f} score={score} size={size_tao:.1f}")

        total_value = portfolio + sum(p.size_tao for p in positions.values())
        equity_curve.append((ts, total_value))

    # ── Results ───────────────────────────────────────────────────────────────
    print("\nSIGNAL FIRE RATES:")
    for model, counts in sorted(signal_stats.items()):
        rate = counts["fired"] / counts["total"] * 100 if counts["total"] else 0
        print(f"  {model:35s} {rate:5.1f}% ({counts['fired']}/{counts['total']})")

    print("\n" + "="*50)
    print("BACKTEST RESULTS")
    print("="*50)
    print(f"Period:        {len(timestamps)} days")
    print(f"Total trades:  {len(closed_trades)}")

    if closed_trades:
        wins     = [t for t in closed_trades if t["pnl_tao"] > 0]
        losses   = [t for t in closed_trades if t["pnl_tao"] <= 0]
        total_pnl = sum(t["pnl_tao"] for t in closed_trades)
        win_rate  = len(wins) / len(closed_trades) * 100
        avg_win   = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss  = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

        gross_win  = sum(t["pnl_tao"] for t in wins)
        gross_loss = abs(sum(t["pnl_tao"] for t in losses))
        profit_factor = gross_win / gross_loss if gross_loss else float("inf")

        sorted_wins = sorted(t["pnl_pct"] for t in wins)
        median_win  = sorted_wins[len(sorted_wins) // 2] if sorted_wins else 0
        sorted_losses = sorted(t["pnl_pct"] for t in losses)
        median_loss = sorted_losses[len(sorted_losses) // 2] if sorted_losses else 0

        exit_reasons = {}
        for t in closed_trades:
            exit_reasons[t["reason"]] = exit_reasons.get(t["reason"], 0) + 1

        streak = max_streak = 0
        for t in sorted(closed_trades, key=lambda x: x["exit_ts"]):
            if t["pnl_tao"] <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0

        print(f"Win rate:      {win_rate:.1f}%")
        print(f"Total P&L:     {total_pnl:+.2f} TAO")
        print(f"Avg win:       {avg_win:+.1f}%  (median {median_win:+.1f}%)")
        print(f"Avg loss:      {avg_loss:+.1f}%  (median {median_loss:+.1f}%)")
        print(f"Profit factor: {profit_factor:.2f}")
        print(f"Max losing streak: {max_streak}")
        print(f"Exit reasons:  " + "  ".join(f"{r}={n}" for r, n in sorted(exit_reasons.items())))
        print(f"Final portfolio: {portfolio + sum(p.size_tao for p in positions.values()):.2f} TAO")
        print(f"Return:        {((portfolio - risk.PORTFOLIO_SIZE_TAO) / risk.PORTFOLIO_SIZE_TAO * 100):+.1f}%")
        print(f"Max drawdown:  {max_drawdown(closed_trades, risk.PORTFOLIO_SIZE_TAO):.1f}%")

        print("\nAll trades:")
        for t in sorted(closed_trades, key=lambda x: x["pnl_tao"], reverse=True):
            print(f"  SN{t['netuid']:3d} {t['reason']:15s} {t['pnl_pct']:+6.1f}% ({t['pnl_tao']:+.2f} TAO)")
            for s in t["entry_signals"]:
                if s.confidence > 0:
                    print(f"    {s.model:35s} score={s.score:+5.1f}  conf={s.confidence:.2f}  {s.reason}")
    else:
        print("No trades executed — entry threshold never hit.")
        print("Consider lowering ENTRY_SCORE_THRESHOLD in risk.py for backtesting.")

    print("="*50)

    if closed_trades and equity_curve:
        _plot_results(equity_curve, closed_trades)


def _plot_results(equity_curve, closed_trades):
    dates  = [datetime.fromtimestamp(ts) for ts, _ in equity_curve]
    values = [v for _, v in equity_curve]

    # Drawdown series
    peak = values[0]
    drawdowns = []
    for v in values:
        if v > peak:
            peak = v
        drawdowns.append((v - peak) / peak * 100)

    # Monthly returns
    from collections import OrderedDict
    monthly = OrderedDict()
    for t in sorted(closed_trades, key=lambda x: x["exit_ts"]):
        month = datetime.fromtimestamp(t["exit_ts"]).strftime("%b %Y")
        monthly[month] = monthly.get(month, 0) + t["pnl_tao"]

    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    fig.suptitle("Axiom Strategy — Backtest Performance (May 2025 – May 2026)", fontsize=13, fontweight="bold")

    # ── 1. Equity curve ──────────────────────────────────────────────────────
    ax = axes[0, 0]
    ax.plot(dates, values, color="#2196F3", linewidth=1.5)
    ax.axhline(100, color="grey", linestyle="--", linewidth=0.8, alpha=0.6)
    ax.set_title("Portfolio Value (TAO)")
    ax.set_ylabel("TAO")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax.grid(alpha=0.3)

    # ── 2. Drawdown ───────────────────────────────────────────────────────────
    ax = axes[0, 1]
    ax.fill_between(dates, drawdowns, 0, color="#F44336", alpha=0.6)
    ax.set_title("Drawdown (%)")
    ax.set_ylabel("%")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")
    ax.grid(alpha=0.3)

    # ── 3. Trade P&L distribution ─────────────────────────────────────────────
    ax = axes[1, 0]
    pnls = [t["pnl_pct"] for t in closed_trades]
    colors = ["#4CAF50" if p > 0 else "#F44336" for p in pnls]
    ax.hist(pnls, bins=30, color="#2196F3", edgecolor="white", linewidth=0.5)
    ax.axvline(0, color="grey", linestyle="--", linewidth=0.8)
    ax.set_title("Trade Return Distribution (%)")
    ax.set_xlabel("Return (%)")
    ax.set_ylabel("# Trades")
    ax.grid(alpha=0.3)

    # ── 4. Monthly P&L ────────────────────────────────────────────────────────
    ax = axes[1, 1]
    months = list(monthly.keys())
    pnl_vals = list(monthly.values())
    bar_colors = ["#4CAF50" if v > 0 else "#F44336" for v in pnl_vals]
    ax.bar(months, pnl_vals, color=bar_colors, edgecolor="white", linewidth=0.5)
    ax.axhline(0, color="grey", linestyle="--", linewidth=0.8)
    ax.set_title("Monthly P&L (TAO)")
    ax.set_ylabel("TAO")
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")
    ax.grid(alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig("backtest_performance.png", dpi=150, bbox_inches="tight")
    print("\nChart saved to backtest_performance.png")
    plt.show()


if __name__ == "__main__":
    run_backtest()
