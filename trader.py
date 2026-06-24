import asyncio
import logging
import time

import store
import risk
import analytics
import notify
import random

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
        # analytics.monte_carlo_emission(netuid)  -- noise, scores +3 on almost everything
   
    ]

    exploring = should_explore()
    if exploring:
        total_weight = sum(s.confidence for s in signals if s.confidence>0)
    else:
        total_weight = sum(s.confidence*store.get_signal_weight(s.model) for s in signals if s.confidence>0)

    
    if total_weight == 0:
        return 0.0, 0.0, []
    if exploring:
        weighted_score = sum(s.score * s.confidence for s in signals if s.confidence>0) / total_weight
    else:
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
    days_held = (int(time.time()) - position["entry_ts"]) / 86400
    if days_held >= risk.MAX_HOLD_DAYS:
        if pnl_pct < risk.TIME_EXIT_MIN_PNL and drawdown_from_peak <= -risk.TIME_EXIT_MAX_DRAWDOWN:
            return True, "time_exit"
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

async def run_weekly_loop():
    import audit
    logger.info("Weekly task loop started...")
    while True:
        await asyncio.sleep(7 * 86400)
        try:
            logger.info("Running weekly decay and audit...")
            decay_signal_weights()
            audit.run_audit()
        except Exception as e:
            logger.error(f"Weekly tasks failed: {e}")

async def _run_cycle():
    ts = int(time.time())
    netuids = store.get_active_netuids()
    open_positions = store.get_all_positions()
    open_netuids = {p["netuid"]: p for p in open_positions}

    deployed_tao = sum(p["size_tao"] for p in open_positions)

    realized_pnl = sum(p["pnl_tao"] or 0 for p in store.get_closed_positions(limit=9999))
    open_snaps = {p["netuid"]: store.get_latest_subnet_snapshot(p["netuid"]) for p in open_positions}
    unrealized_pnl = sum(
        (open_snaps[p["netuid"]]["alpha_price_tao"] - p["entry_price"]) / p["entry_price"] * p["size_tao"]
        for p in open_positions
        if open_snaps.get(p["netuid"]) and open_snaps[p["netuid"]].get("alpha_price_tao")
    )
    portfolio_value = risk.PORTFOLIO_SIZE_TAO + realized_pnl + unrealized_pnl

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

        should_exit, reason = check_exit(position, current_price, score)

        if should_exit:
            closed = store.get_position(netuid)
            store.close_positions(netuid, ts, current_price, reason)
            if closed:
                update_signal_weights(closed[0], current_price)
            pnl_pct = (current_price - position["entry_price"]) / position["entry_price"] * 100
            logger.info(f"Exited SN{netuid} ({reason}) at {current_price:.4f}")
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            await notify.send(
                f"{emoji} <b>Exited SN{netuid}</b> — {reason}\n"
                f"Entry: {position['entry_price']:.4f}  Exit: {current_price:.4f}  P&L: <b>{pnl_pct:+.1f}%</b>",
                parse_mode="HTML",
            )
        else:
            trailing_trigger = position["peak_price"] * (1 - risk.TRAILING_STOP)
            if current_price < trailing_trigger * 1.03 and current_price > trailing_trigger:
                await notify.send(
                    f"⚠️ <b>SN{netuid}</b> near trailing stop\n"
                    f"Current: {current_price:.4f}  Trigger: {trailing_trigger:.4f}  Peak: {position['peak_price']:.4f}",
                    parse_mode="HTML",
                )
            pnl_pct = (current_price - position["entry_price"]) / position["entry_price"]
            if pnl_pct >= risk.TAKE_PROFIT * 0.85:
                await notify.send(
                    f"🎯 <b>SN{netuid}</b> approaching take profit\n"
                    f"P&L: {pnl_pct*100:+.1f}%  Target: {risk.TAKE_PROFIT*100:.0f}%  Current: {current_price:.4f}",
                    parse_mode="HTML",
                )

    for i, netuid in enumerate(netuids):
        logger.info(f"Scoring SN{netuid} ({i+1}/{len(netuids)})")
        if netuid in open_netuids:
            continue
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

        at_capacity = len(open_netuids) >= risk.MAX_OPEN_POSITIONS
        if at_capacity:
            weakest = min(open_netuids, key=lambda n: (store.get_recent_signals(n, 1) or [{"score": float("inf")}])[0]["score"])
            weakest_score = (store.get_recent_signals(weakest, 1) or [{"score": float("inf")}])[0]["score"]
            if score <= weakest_score + risk.REPLACEMENT_MARGIN:
                continue
            weakest_days_held = (int(time.time()) - open_netuids[weakest]["entry_ts"]) / 86400
            if weakest_days_held < risk.MIN_HOLD_DAYS:
                continue
            weakest_snap = store.get_latest_subnet_snapshot(weakest)
            weakest_pnl = 0.0
            if weakest_snap and weakest_snap.get("alpha_price_tao"):
                weakest_entry = open_netuids[weakest]["entry_price"]
                weakest_pnl = (weakest_snap["alpha_price_tao"] - weakest_entry) / weakest_entry
            if weakest_pnl >= risk.EVICT_MIN_PNL:
                continue
            evict_snap = store.get_latest_subnet_snapshot(weakest)
            if not evict_snap or not evict_snap.get("alpha_price_tao"):
                continue
            evict_price = evict_snap["alpha_price_tao"]
            evict_pos = store.get_position(weakest)
            store.close_positions(weakest, ts, evict_price, "replaced")
            if evict_pos:
                update_signal_weights(evict_pos[0], evict_price)
            open_netuids.pop(weakest)
            logger.info(f"Evicted SN{weakest} (score {weakest_score:.2f}) for SN{netuid} (score {score:.2f})")
            await notify.send(
                f"🔄 Replaced <b>SN{weakest}</b> (score {weakest_score:.2f}) with <b>SN{netuid}</b> (score {score:.2f})",
                parse_mode="HTML",
            )

        score_range = max(5.0 - risk.ENTRY_SCORE_THRESHOLD, 0.01)
        scale = min((score - risk.ENTRY_SCORE_THRESHOLD) / score_range, 1.0)
        size_tao = portfolio_value * (risk.MIN_POSITION_SIZE + (risk.MAX_POSITION_SIZE - risk.MIN_POSITION_SIZE) * scale)

        if deployed_tao + size_tao > portfolio_value * risk.MAX_TOTAL_DEPLOYED:
            continue

        if portfolio_value - deployed_tao - size_tao < risk.MIN_TAO_BALANCE:
            continue

        store.open_positions(ts, netuid, current_price, size_tao)
        open_netuids[netuid] = {"netuid": netuid, "entry_price": current_price}
        deployed_tao += size_tao
        logger.info(f"Opened position in SN{netuid} with size {size_tao:.2f} TAO at price {current_price:.2f} with score {score} and confidence {confidence}")
        await notify.send(
            f"🟢 <b>Entered SN{netuid}</b> @ {current_price:.4f}\n"
            f"Size: {size_tao:.1f} TAO  Score: {score:.2f}  Confidence: {confidence:.2f}",
            parse_mode="HTML",
        )


def positions_context() -> str:
    open_positions = store.get_all_positions()
    closed_recent = store.get_closed_positions(limit=10)

    lines = []

    if open_positions:
        lines.append("OPEN POSITIONS:")
        for p in open_positions:
            snap = store.get_latest_subnet_snapshot(p["netuid"])
            cur = snap["alpha_price_tao"] if snap and snap.get("alpha_price_tao") else None
            age_h = (int(time.time()) - p["entry_ts"]) / 3600
            if cur:
                pnl_pct = (cur - p["entry_price"]) / p["entry_price"] * 100
                drawdown = (cur - p["peak_price"]) / p["peak_price"] * 100
                trailing_trigger = p["peak_price"] * (1 - risk.TRAILING_STOP)
                lines.append(
                    f"  SN{p['netuid']}: entry {p['entry_price']:.4f} | current {cur:.4f} | "
                    f"P&L {pnl_pct:+.1f}% | peak {p['peak_price']:.4f} | "
                    f"drawdown from peak {drawdown:+.1f}% | trailing stop trigger {trailing_trigger:.4f} | "
                    f"age {age_h:.0f}h | size {p['size_tao']:.1f} TAO"
                )
            else:
                lines.append(f"  SN{p['netuid']}: entry {p['entry_price']:.4f} | no current price | age {age_h:.0f}h")
    else:
        lines.append("OPEN POSITIONS: none")

    deployed = sum(p["size_tao"] for p in open_positions)
    realized = sum(p["pnl_tao"] or 0 for p in closed_recent)
    lines.append(f"\nDeployed: {deployed:.1f} TAO across {len(open_positions)} positions")

    if closed_recent:
        lines.append("\nRECENT EXITS (last 10):")
        for p in closed_recent:
            lines.append(
                f"  SN{p['netuid']}: {p['exit_reason']} | P&L {p['pnl_tao']:+.4f} TAO | "
                f"entry {p['entry_price']:.4f} -> exit {p['exit_price']:.4f}"
            )
        lines.append(f"\nRealized P&L (last 10 exits): {realized:+.4f} TAO")

    return "\n".join(lines)


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

LEARNING_RATE = 0.15 
WEIGHT_DECAY = 0.05 
BASELINE_WINDOW = 10
EXPLORATION_RATE = 0.10
MIN_WEIGHT = 0.1
MAX_WEIGHT = 2.0

def _compute_reward(positon, exit_price):
    pnl_pct = (exit_price - positon["entry_price"]) / positon["entry_price"]
    days_held = (int(time.time()) - positon["entry_ts"]) / 86400
    return pnl_pct / max(days_held, 1)
def _compute_baseline():
    closed = store.get_closed_positions(limit=BASELINE_WINDOW)
    if not closed:
        return 0.0
    rewards = []
    for p in closed:
        if p.get("exit_price") and p.get("entry_price"):
            pnl = (p["exit_price"] - p["entry_price"]) / p["entry_price"]
            days = max((p.get("exit_ts", 0) - p.get("entry_ts", 0)) / 86400, 1)
            rewards.append(pnl / days)
    return sum(rewards) / len(rewards) if rewards else 0.0

def update_signal_weights(position, exit_price):
    reward = _compute_reward(position, exit_price)
    baseline = _compute_baseline()
    advantage = reward - baseline 

    signals_at_entry = store.get_model_signals_at_time(position["netuid"], position["entry_ts"])
    if not signals_at_entry:
        return
    total_weight = sum(
          s["score"] * s["confidence"] * store.get_signal_weight(s["model"])
          for s in signals_at_entry if s["confidence"] > 0
      )
    if total_weight == 0:
        return
    for s in signals_at_entry:
        if s["confidence"] <= 0:
            continue
        current_weight = store.get_signal_weight(s["model"])
        contribution = (s["score"] * s["confidence"] * current_weight) / total_weight
        new_weight = current_weight + LEARNING_RATE * advantage * contribution
        new_weight = max(MIN_WEIGHT, min(MAX_WEIGHT, new_weight))
        store.update_signal_weight(s["model"], new_weight)
        logger.info(f"Updated weight for model {s['model']}: {current_weight:.2f} -> {new_weight:.2f} (reward: {reward:.4f}, baseline: {baseline:.4f})")


def decay_signal_weights():
    weights = store.get_all_signal_weights()
    for model, weight in weights.items():
        decayed = weight + WEIGHT_DECAY * (1.0 - weight)
        decayed = max(MIN_WEIGHT, min(MAX_WEIGHT, decayed))
        store.update_signal_weight(model, decayed)

def should_explore():
    return random.random() < EXPLORATION_RATE


async def run_daily_summary_loop():
    from datetime import datetime, timezone, timedelta
    logger.info("Daily summary loop started...")
    while True:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((tomorrow - now).total_seconds())
        try:
            await _send_daily_summary()
        except Exception as e:
            logger.error(f"Daily summary failed: {e}")


async def _send_daily_summary():
    from datetime import datetime, timezone
    today_start = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    date_str = datetime.now(timezone.utc).strftime("%b %d")

    open_positions = store.get_all_positions()
    closed_today = [p for p in store.get_closed_positions(limit=50) if (p.get("exit_ts") or 0) >= today_start]
    entries_today = [p for p in open_positions if p["entry_ts"] >= today_start]

    unrealized = 0.0
    rows = []
    for p in open_positions:
        snap = store.get_latest_subnet_snapshot(p["netuid"])
        price = snap["alpha_price_tao"] if snap and snap.get("alpha_price_tao") else None
        pnl_pct = 0.0
        if price:
            pnl_pct = (price - p["entry_price"]) / p["entry_price"] * 100
            unrealized += pnl_pct / 100 * p["size_tao"]
        rows.append((p["netuid"], p["size_tao"], p["entry_price"], price, pnl_pct))

    deployed = sum(p["size_tao"] for p in open_positions)
    day_pnl = sum(p["pnl_tao"] or 0 for p in closed_today)

    lines = [f"📊 <b>Daily Summary — {date_str}</b>", ""]
    lines.append(f"Open: {len(open_positions)} positions | {deployed:.1f} TAO deployed")
    lines.append(f"Unrealized: <b>{unrealized:+.2f} TAO</b>")

    if rows:
        rows.sort(key=lambda r: -r[4])
        table = f"{'SN':<5}{'Entry':>7}{'Now':>7}{'P&L':>7}\n"
        for netuid, size, entry, price, pnl_pct in rows:
            price_str = f"{price:.3f}" if price is not None else "-"
            table += f"SN{netuid:<3}{entry:>7.3f}{price_str:>7}{pnl_pct:>+6.1f}%\n"
        lines.append(f"\n<pre>{table}</pre>")

    if entries_today:
        lines.append("<b>Entered today:</b>")
        for p in entries_today:
            lines.append(f"• SN{p['netuid']} @ {p['entry_price']:.4f}")

    if closed_today:
        lines.append("\n<b>Exited today:</b>")
        for p in closed_today:
            lines.append(f"• SN{p['netuid']} {p['exit_reason']} | {p['pnl_tao']:+.4f} TAO")
        lines.append(f"\nRealized today: <b>{day_pnl:+.4f} TAO</b>")
    else:
        lines.append("\nNo exits today.")

    await notify.send("\n".join(lines), parse_mode="HTML")