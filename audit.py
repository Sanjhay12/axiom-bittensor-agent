import time
import logging
import store 

logger = logging.getLogger(__name__)

AUDIT_WINDOW_DAYS   = 30
DECAY_THRESHOLD     = 0.45   # win rate below this → DECAYED
IMPROVING_THRESHOLD = 0.62   # win rate above this → IMPROVING
DECAYED_WEIGHT_CAP  = 0.3
IMPROVING_WEIGHT_FLOOR = 0.8

def _get_closed_trades(days=30):
      cutoff = int(time.time()) - days * 86400
      with store.get_conn() as conn:
          return store._rows(conn, """
              SELECT id, netuid, entry_ts, exit_ts, entry_price, exit_price
              FROM paper_positions
              WHERE status != 'open'
              AND exit_price IS NOT NULL
              AND entry_ts > %s
          """, (cutoff,))


def _signal_would_have_entered(model, netuid, entry_ts, threshold=0.0):
      with store.get_conn() as conn:
          rows = store._rows(conn, """
              SELECT score FROM model_signal_history
              WHERE netuid = %s AND model = %s AND ts <= %s
              ORDER BY ts DESC LIMIT 1
          """, (netuid, model, entry_ts))
      if not rows:
          return False
      return rows[0]["score"] > threshold


def audit_signal(model, trades):
    approved = []
    for  trade in trades:
          if _signal_would_have_entered(model, trade["netuid"], trade["entry_ts"]):
               pnl = (trade["exit_price"] - trade["entry_price"]) / trade["entry_price"]
               approved.append(pnl)
    if not approved:
          return {
               "model": model,
               "approved_trades": 0,
                "win_rate": 0.0,
                "avg_return": 0.0,
                "trend": "N/A",
          }
    wins = sum(1 for p in approved if p > 0)
    win_rate = wins / len(approved)
    avg_return = sum(approved) / len(approved)
    if win_rate < DECAY_THRESHOLD:
          trend = "DECAYED"
    elif win_rate > IMPROVING_THRESHOLD:
          trend = "IMPROVING"
    else:
          trend = "STABLE" if avg_return >= 0 else "WEAKENING"
    return {
         "model": model,
         "approved_trades": len(approved),
            "win_rate": round(win_rate, 2),
            "avg_return": round(avg_return, 4),
            "trend": trend,
    } 

def _apply_weight_constraints(results):
     for r in results:
          if r["trend"] == "DECAYED":
               current = store.get_signal_weight(r["model"])
               if current > DECAYED_WEIGHT_CAP:
                    store.update_signal_weight(r["model"], DECAYED_WEIGHT_CAP)
                    logger.info(f"Decayed weight of model {r['model']} from {current:.2f} to {DECAYED_WEIGHT_CAP:.2f} due to audit")
          elif r["trend"] == "IMPROVING":
               current = store.get_signal_weight(r["model"])
               if current < IMPROVING_WEIGHT_FLOOR:
                    store.update_signal_weight(r["model"], IMPROVING_WEIGHT_FLOOR)
                    logger.info(f"Improved weight of model {r['model']} from {current:.2f} to {IMPROVING_WEIGHT_FLOOR:.2f} due to audit")


def run_audit():
      trades = _get_closed_trades(days=AUDIT_WINDOW_DAYS)
      if len(trades) < 5:
          logger.info("Audit skipped — insufficient trade history (need at least 5 closed trades)")
          return None

      models = [
          "emission_price_divergence",
          "alpha_price_momentum",
          "zscore_anomaly",
          "regime_detection",
          "death_spiral_warning",
          "stake_concentration_gini",
          "kalman_filter_price",
          "registration_cost_velocity",
          "isolation_forest_anomaly",
      ]

      results = [audit_signal(model, trades) for model in models]
      results.sort(key=lambda r: r["win_rate"], reverse=True)

      _apply_weight_constraints(results)

      # log report
      logger.info("=" * 55)
      logger.info(f"SIGNAL AUDIT — last {AUDIT_WINDOW_DAYS} days — {len(trades)} trades")
      logger.info("=" * 55)
      for r in results:
          logger.info(
              f"{r['model']:<35} "
              f"win={r['win_rate']:.0%}  "
              f"avg={r['avg_return']:+.2%}  "
              f"n={r['approved_trades']}  "
              f"{r['trend']}"
          )
      logger.info("=" * 55)

      return results


if __name__ == "__main__":
      import logging
      logging.basicConfig(level=logging.INFO)
      from dotenv import load_dotenv
      load_dotenv()
      run_audit()