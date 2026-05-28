import asyncio
import logging
import time

import store
import risk
import analytics

logger = logging.getLogger(__name__)


def init_db():
    with store.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS paper_positions (
                    id              SERIAL PRIMARY KEY,
                    netuid          INTEGER NOT NULL,
                    entry_ts        INTEGER NOT NULL,
                    entry_price     REAL NOT NULL,
                    size_tao        REAL NOT NULL,
                    peak_price      REAL NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'open',
                    exit_ts         INTEGER,
                    exit_price      REAL,
                    exit_reason     TEXT,
                    pnl_tao         REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signal_history (
                    id          SERIAL PRIMARY KEY,
                    ts          INTEGER NOT NULL,
                    netuid      INTEGER NOT NULL,
                    score       REAL NOT NULL,
                    confidence  REAL NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS model_signal_history (
                    id         SERIAL PRIMARY KEY,
                    ts         INTEGER NOT NULL,
                    netuid     INTEGER NOT NULL,
                    model      TEXT NOT NULL,
                    score      REAL NOT NULL,
                    confidence REAL NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signal_weights (
                    model   TEXT PRIMARY KEY,
                    weight  REAL NOT NULL DEFAULT 1.0
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_positions_netuid ON paper_positions(netuid, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_netuid ON signal_history(netuid, ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_model_signals ON model_signal_history(netuid, ts)")


def score_subnet(netuid):
    signals = [
        analytics.emission_price_divergence(netuid),
        analytics.alpha_price_momentum(netuid),
        analytics.zscore_anomaly(netuid),
        analytics.regime_detection(netuid),
        analytics.death_spiral_warning(netuid),
        analytics.stake_concentration_gini(netuid),
        analytics.kalman_filter_price(netuid),
        analytics.registration_cost_velocity(netuid),
        analytics.isolation_forest_anomaly(netuid),
        analytics.monte_carlo_emission(netuid)
    ]

    total_weight = sum(s.confidence*store.get_signal_weight(s.model) for s in signals if s.confidence>0)
    if total_weight == 0:
        return 0.0, 0.0, []
    weighted_score = sum(s.score * s.confidence*store.get_signal_weight(s.model) for s in signals if s.confidence>0) / total_weight
    avg_confidence = total_weight / len(signals)

    return round(weighted_score, 2), round(avg_confidence, 2), signals

def check_entry(netuid: int, current_score: float):
    if current_score < risk.ENTRY_SCORE_THRESHOLD:
        return False
    recent = store.get_recent_signals(netuid, risk.ENTRY_CYCLES_REQUIRED)
    if len(recent) < risk.ENTRY_CYCLES_REQUIRED:
        return False
    return all(s["score"] > risk.ENTRY_SCORE_THRESHOLD for s in recent)

def check_exit(position: dict, current_price: float, current_score: float):
    entry_price = position["entry_price"]
    peak_price = position["peak_price"]
    pnl_pct = (current_price - entry_price) / entry_price
    if pnl_pct <= -risk.STOP_LOSS:
        return True, "stop_loss"
    if pnl_pct >= risk.TAKE_PROFIT:
        return True, "take_profit"
    drawdown_from_peak = (current_price - peak_price) / peak_price
    if drawdown_from_peak <= -risk.TRAILING_STOP:
        return True, "trailing_stop"
    if current_score < risk.EXIT_SCORE_THRESHOLD:
        return True, "signal_exit"
    return False, ""

async def run_loop():
    init_db()
    logger.info("Starting trader loop...")
    while True:
        try:
            await _run_cycle()
        except Exception as e:
            logger.error(f"Error in trader loop: {e}")
        await asyncio.sleep(14400)  # run every hour

async def _run_cycle():
    ts = int(time.time())
    netuids = store.get_active_netuids()
    open_positions = store.get_all_positions()
    open_netuids = {p["netuid"]: p for p in open_positions}

    deployed_tao = sum(p["size_tao"] for p in open_positions)
    portfolio_value = risk.PORTFOLIO_SIZE_TAO

    logger.info(f"Cycle started — {len(netuids)} subnets, {len(open_positions)} open positions")

    for position in open_positions:
        netuid = position["netuid"]
        snapshot = store.get_latest_subnet_snapshot(netuid)
        if not snapshot or not snapshot.get("alpha_price_tao"):
            continue 
        current_price = snapshot["alpha_price_tao"]
        store.update_peak_price(netuid, current_price)

        score, confidence, signals = score_subnet(netuid)
        store.insert_signals(ts, netuid, score, confidence)
        store.insert_model_signals(ts, netuid, signals)

        should_exit, reason = check_exit(position, current_price, score )

        if should_exit:
            closed = store.get_position(netuid)
            store.close_positions(netuid, ts, current_price, reason)
            if closed:
                update_signal_weights(closed[0], current_price)
            logger.info(f"Exited position in SN{netuid} for reason: {reason}" + "at price" + f"{current_price:.2f}")

    for i, netuid in enumerate(netuids):
        logger.info(f"Scoring SN{netuid} ({i+1}/{len(netuids)})")
        if netuid in open_netuids:
            continue
        if len(open_netuids) >= risk.MAX_OPEN_POSITIONS:
            break
        snapshot = store.get_latest_subnet_snapshot(netuid)
        if not snapshot or not snapshot.get("alpha_price_tao"):
            continue
        current_price = snapshot["alpha_price_tao"]
        score, confidence, signals = score_subnet(netuid)
        store.insert_signals(ts, netuid, score, confidence)
        store.insert_model_signals(ts, netuid, signals)

        if deployed_tao / portfolio_value >= risk.MAX_TOTAL_DEPLOYED:
            break

        if not check_entry(netuid, score):
            continue
        size_tao = portfolio_value * (0.15 if score>=8 else 0.08)
        size_tao = min(size_tao, portfolio_value * risk.MAX_POSITION_SIZE)

        if deployed_tao + size_tao > portfolio_value * risk.MAX_TOTAL_DEPLOYED:
            continue 

        if portfolio_value - deployed_tao-size_tao < risk.MIN_TAO_BALANCE:
            continue

        store.open_positions(ts, netuid, current_price, size_tao)
        deployed_tao+= size_tao
        logger.info(f"Opened position in SN{netuid} with size {size_tao:.2f} TAO at price {current_price:.2f} with score {score} and confidence {confidence}")


def positions_summary() -> str:
    positions = store.get_all_positions()
    if not positions:
        return "No open positions."
    lines = []
    for p in positions:
        current_snapshot = store.get_latest_subnet_snapshot(p["netuid"])
        current_price = current_snapshot["alpha_price_tao"] if current_snapshot else 0
        pnl_pct = ((current_price - p["entry_price"]) / p["entry_price"] * 100) if current_price else 0
        lines.append(
            f"SN{p['netuid']} — {p['size_tao']:.1f} TAO @ {p['entry_price']:.4f} "
            f"| now {current_price:.4f} | P&L {pnl_pct:+.1f}%"
        )
    deployed = sum(p["size_tao"] for p in positions)
    lines.append(f"\nOpen: {len(positions)} positions | Deployed: {deployed:.1f} TAO")
    return "\n".join(lines)

def update_signal_weights(position, exit_price):
    outcome = (exit_price - position["entry_price"]) / position["entry_price"]
    signals_at_entry = store.get_model_signals_at_time(position["netuid"], position["entry_ts"])
    for s in signals_at_entry:
        current_weight = store.get_signal_weight(s["model"])
        new_weight = current_weight + 0.1*(outcome * s["score"])
        new_weight = max(0.1, min(2.0, new_weight))
        store.update_signal_weight(s["model"], new_weight)
        
