import time
import json

import store
import trader
import anthropic
import os
import insurance_store
import logging 

logging.basicConfig(level=logging.INFO)

TRIGGER_BASE_RATES = {
    "death_spiral":       0.006,
      "emission_collapse":  0.005,
      "regime_change":      0.004,
      "alpha_price_crash":  0.007,
      "catastrophic_churn": 0.003,
      "composite_risk":     0.012,
}

MIN_POOL_SIZE = 50.0 
MAX_UTILIZATION = 2.0 
MAX_CONCENTRATION = 0.20 
MIN_HISTORY_DAYS = 14

def _risk_multiplier(score: float):
    if score > 4:
        return 0.2 
    elif score < -2:
        return 3.0 
    return 1.0 

def underwrite(netuid, coverage_amount,  period_days, trigger_type):
    if trigger_type not in TRIGGER_BASE_RATES:
        return {"approved": False, "reason": "Invalid trigger type"}
    
    history = store.get_subnet_history(netuid, days=MIN_HISTORY_DAYS)
    if len(history) < MIN_HISTORY_DAYS:
        return {"approved": False, "reason": "Insufficient history"}
    
    pool = insurance_store.get_pool_state()
    if pool["total_pool_size"] < MIN_POOL_SIZE:
        return {"approved": False, "reason": "Pool too small"}

    new_utilization = (pool["total_coverage"] + coverage_amount) / pool["total_pool_size"]
    if new_utilization > MAX_UTILIZATION:
        return {"approved": False, "reason": "Pool over-utilized"}

    subnet_coverage = insurance_store.get_subnet_coverage(netuid)
    new_concentration = (subnet_coverage + coverage_amount) / pool["total_pool_size"]
    if new_concentration > MAX_CONCENTRATION:
        return {"approved": False, "reason": "Subnet over-concentrated"}
    
    score, confidence, signals = trader.score_subnet(netuid)
    multiplier = _risk_multiplier(score)
    base_rate = TRIGGER_BASE_RATES[trigger_type]
    premium = round(base_rate * coverage_amount * (period_days / 30) * multiplier, 4)
    
    return {"approved": True, "premium": premium, "risk_multiplier": multiplier, "score": score, "confidence": confidence, "signals": signals, "coverage_amount": coverage_amount, "period_days": period_days, "trigger_type": trigger_type, "quoted_ts": int(time.time()), "expires_ts": int(time.time()) + period_days * 24 * 3600}


claude = anthropic.AsyncAnthropic(os.getenv("ANTHROPIC_API_KEY"))
_ADVERSE_SELECTION_PROMPT = """
  You are an insurance underwriter reviewing a policy application for a Bittensor subnet.
  Your job is to detect adverse selection — someone applying for coverage on an event that is already
  developing.

  Subnet: SN{netuid}
  Trigger requested: {trigger_type}
  Current signal score: {score} (range -10 to +10, negative is bad)
  Risk multiplier: {multiplier}x

  Current signal breakdown:
  {signal_breakdown}

  Score trend over last 7 days:
  {score_trend}

  Based on the above, does this application show signs of adverse selection?
  Look for: score trending sharply negative, the specific trigger type already showing early signals, sudden
  application after a deterioration event.

  Respond in JSON only:
  {{
    "decision": "approve" | "flag" | "decline",
    "reason": "<one sentence>"
  }}
"""

async def check_adverse_selection(netuid, underwrite_result):
    recent_scores = store.get_recent_signals(netuid, cycles=42)  # ~7 days
    score_trend = [round(r["score"], 2) for r in recent_scores]

    signal_breakdown = "\n".join(
        f"{s.model}: score={s.score}, confidence={s.confidence}, reason={s.reason}"
        for s in underwrite_result["signals"]
    )

    prompt = _ADVERSE_SELECTION_PROMPT.format(
        netuid=netuid,
        trigger_type=underwrite_result["trigger_type"],
        score=underwrite_result["score"],
        multiplier=underwrite_result["risk_multiplier"],
        signal_breakdown=signal_breakdown,
        score_trend=score_trend,
    )

    result = await claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )

    text = result.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    return json.loads(text)


async def issue_quote(netuid, coverage_amount, period_days, trigger_type):
    result = underwrite(netuid, coverage_amount, period_days, trigger_type)
    if not result["approved"]:
        return result

    assessment = await check_adverse_selection(netuid, result)

    if assessment["decision"] == "decline":
        return {"approved": False, "reason": f"adverse selection detected: {assessment['reason']}"}

    if assessment["decision"] == "flag":
        result["risk_multiplier"] = min(result["risk_multiplier"] * 1.5, 3.0)
        result["premium"] = round(
            TRIGGER_BASE_RATES[trigger_type] * result["risk_multiplier"] *
            coverage_amount * (period_days / 30), 4
        )
        result["flagged"] = True
        result["flag_reason"] = assessment["reason"]

    return result


TRIGGER_CHECKS = {}
def _check_death_spiral(netuid):
    recent = store.get_recent_model_signals(netuid, model="death_spiral_warning", cycles=3)
    if len(recent) < 2:
        return False 
    return all(r["score"] < -5 for r in recent[:2])

def _check_emission_collapse(netuid):
    rows = store.get_subnet_history(netuid, days=7)
    rows = [r for r in rows if r.get("total_emission_tao") is not None]
    if len(rows) < 2:
        return False
    change = (rows[-1]["total_emission_tao"] - rows[0]["total_emission_tao"]) / rows[0]["total_emission_tao"]
    return change < -0.5

def _check_regime_change(netuid):
    recent = store.get_recent_model_signals(netuid, model="regime_detection", cycles=3)
    if len(recent) < 2:
        return False 
    return all(r["score"] < -3 for r in recent[:3])

def _check_alpha_price_crash(netuid):
    rows = store.get_alpha_price_history(netuid, days=14)
    if len(rows) < 2:
        return False
    change = (rows[-1]["alpha_price_tao"] - rows[0]["alpha_price_tao"]) / rows[0]["alpha_price_tao"]
    return change < -0.4

def _check_catastrophic_churn(netuid):
    recent = store.get_churn_history(netuid, days=1)
    if not recent:
        return False
    return recent[-1].get("deregistered_count",0) >= 6

def _check_composite_risk(netuid):
    recent = store.get_recent_signals(netuid, cycles = 3)
    if len(recent) < 2:
        return False
    return all(r["score"] < -5 for r in recent[:2])

TRIGGER_CHECKS = {
    "death_spiral": _check_death_spiral,
    "emission_collapse": _check_emission_collapse,
    "regime_change": _check_regime_change,
    "alpha_price_crash": _check_alpha_price_crash,
    "catastrophic_churn": _check_catastrophic_churn,
    "composite_risk": _check_composite_risk,
}


def assess_claims():
    insurance_store.expire_policies()
    policies = insurance_store.get_active_policies()
    if not policies:
        logging.info("No active policies to assess")
        return
    logging.info(f"Assessing {len(policies)} active policies for claims")

    for policy in policies:
        netuid = policy["netuid"]
        trigger_type = policy["trigger_type"]
        policy_id = policy["id"]

        score, confidence, signals = trader.score_subnet(netuid)
        check = TRIGGER_CHECKS.get(trigger_type)
        if not check:
            continue 
        triggered = check(netuid) 

        if triggered:
              insurance_store.payout_policy(policy_id)
              insurance_store.log_audit(
                  policy_id=policy_id,
                  event_type="payout",
                  decision="paid_out",
                  reason=f"{trigger_type} confirmed",
                  signals=signals,
                  cycle_score=score
              )
              logging.info(f"Policy {policy_id} triggered — {trigger_type} on SN{netuid}")

        elif score < -1:
            insurance_store.log_audit(
                  policy_id=policy_id,
                  event_type="claims_check",
                  decision="watching",
                  reason=f"conditions developing, not yet confirmed (score={score:.2f})",
                  signals=signals,
                  cycle_score=score )



def run_pool_cycle(last_cycle_ts):
    ts = int(time.time())

    with insurance_store.get_conn() as conn:
        new_policies = insurance_store._rows(conn, """
            SELECT id, premium FROM insurance_policies
            WHERE status = 'active' AND activated_ts > %s
        """, (last_cycle_ts,))

    total_premiums = sum(p["premium"] for p in new_policies)

    if total_premiums > 0:
        for p in new_policies:
            insurance_store.log_transaction(ts, "premium_in", p["premium"], policy_id=p["id"])

        agent_fee = round(total_premiums * 0.10, 6)
        insurance_store.log_transaction(ts, "agent_fee", agent_fee)

        lp_pool = round(total_premiums * 0.90, 6)
        pool = insurance_store.get_pool_state()

        if pool["total_pool_size"] > 0:
            with insurance_store.get_conn() as conn:
                deposits = insurance_store._rows(conn, """
                    SELECT id, depositor_id, amount_tao FROM lp_deposits
                    WHERE withdrawn = FALSE
                """)
            for deposit in deposits:
                share = deposit["amount_tao"] / pool["total_pool_size"]
                cut = round(lp_pool * share, 6)
                if cut > 0:
                    with insurance_store.get_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE lp_deposits SET amount_tao = amount_tao + %s
                                WHERE id = %s
                            """, (cut, deposit["id"]))
                    insurance_store.log_transaction(
                        ts, "distribution", cut,
                        depositor_id=deposit["depositor_id"]
                    )

    # payout deductions
    with insurance_store.get_conn() as conn:
        triggered = insurance_store._rows(conn, """
            SELECT id, coverage_amount FROM insurance_policies
            WHERE status = 'paid_out' AND payout_ts > %s
        """, (last_cycle_ts,))

    for policy in triggered:
        coverage = policy["coverage_amount"]
        pool = insurance_store.get_pool_state()
        if pool["total_pool_size"] <= 0:
            break
        with insurance_store.get_conn() as conn:
            deposits = insurance_store._rows(conn, """
                SELECT id, depositor_id, amount_tao FROM lp_deposits
                WHERE withdrawn = FALSE
            """)
        for deposit in deposits:
            share = deposit["amount_tao"] / pool["total_pool_size"]
            loss = round(coverage * share, 6)
            with insurance_store.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE lp_deposits SET amount_tao = GREATEST(amount_tao - %s, 0)
                        WHERE id = %s
                    """, (loss, deposit["id"]))
            insurance_store.log_transaction(
                ts, "payout_deduction", loss,
                policy_id=policy["id"],
                depositor_id=deposit["depositor_id"]
            )