import time 
import logging

from torch import threshold 
import insurance_store 

logging.basicConfig(level=logging.INFO)
DEPOSIT_MAX_UTILIZATION  = 0.50
DEPOSIT_MIN_APR          = 0.15
WITHDRAW_MIN_UTILIZATION = 1.75
WITHDRAW_MAX_APR         = 0.05
WITHDRAW_PAYOUT_WINDOW   = 48 * 3600
WITHDRAW_POOL_DROP       = 0.20

def _get_apr():
      with insurance_store.get_conn() as conn:
          rows = insurance_store._rows(conn, """
              SELECT COALESCE(SUM(amount_tao), 0) AS total
              FROM pool_transactions
              WHERE tx_type = 'premium_in'
              AND ts > %s
          """, (int(time.time()) - 365 * 86400,))
      annual_premiums = rows[0]["total"] * 0.90
      pool = insurance_store.get_pool_state()
      if pool["total_pool_size"] == 0:
          return 0.0
      return annual_premiums / pool["total_pool_size"]

def _recent_payout(window_seconds):
      with insurance_store.get_conn() as conn:
          rows = insurance_store._rows(conn, """
              SELECT COUNT(*) AS cnt FROM pool_transactions
              WHERE tx_type = 'payout_deduction'
              AND ts > %s
          """, (int(time.time()) - window_seconds,))
      return rows[0]["cnt"] > 0


def _pool_dropped_since_deposit(depositor_id, threshold):
     deposits = insurance_store.get_lp_deposits(depositor_id)
     if not deposits:
         return False
     earliest = min(deposits, key = lambda d: d["deposit_ts"])
     original = earliest["amount_tao"]
     current = sum(d["amount_tao"] for d in deposits)
     if original == 0:
         return False
     drop = (original - current) / original
     return drop > threshold

def _withdraw_all(depositor_id, unlocked_deposits, reason):
     pool = insurance_store.get_pool_state()
     for deposit in unlocked_deposits:
          share = deposit["amount_tao"] / pool["total_pool_size"] if pool["total_pool_size"] > 0 else 0.0
          amount =round(share * pool["total_pool_size"], 6)
          insurance_store.withdraw_deposit(deposit["id"], amount)
          logging.info(f"Withdrew {amount} TAO for depositor {depositor_id} due to {reason}")

def run_autonomous(depositor_id, deposit_amount):
    pool = insurance_store.get_pool_state()
    utilization = (pool["total_coverage"]/pool["total_pool_size"]) if pool["total_pool_size"] > 0 else 0.0
    apr = _get_apr()
    recent_payout = _recent_payout(WITHDRAW_PAYOUT_WINDOW)
    pool_dropped = _pool_dropped_since_deposit(depositor_id, WITHDRAW_POOL_DROP)
    deposits = insurance_store.get_lp_deposits(depositor_id)
    unlocked = [d for d in deposits if d["lockup_expires"] < int(time.time()) and not d["withdrawn"]]
    if unlocked:
          if utilization > WITHDRAW_MIN_UTILIZATION:
              _withdraw_all(depositor_id, unlocked, "utilization too high")
              return
          if apr < WITHDRAW_MAX_APR:
              _withdraw_all(depositor_id, unlocked, "APR too low")
              return
          if recent_payout:
              _withdraw_all(depositor_id, unlocked, "payout in last 48 hours")
              return
          if pool_dropped:
              _withdraw_all(depositor_id, unlocked, "pool dropped since deposit")
              return
          
          
    if (utilization < DEPOSIT_MAX_UTILIZATION and apr > DEPOSIT_MIN_APR and not recent_payout):
        insurance_store.create_deposit(depositor_id, deposit_amount)
        logging.info(f"Created deposit of {deposit_amount} TAO for depositor {depositor_id}")
        return 
    logging.info(f"No action taken for depositor {depositor_id}. Utilization: {utilization:.2f}, APR: {apr:.2f}, Recent Payout: {recent_payout}, Pool Drop: {pool_dropped}")


