from dataclasses import dataclass, field
from sklearn.ensemble import IsolationForest
from hmmlearn.hmm import GaussianHMM
import numpy as np
import random
import store
import numpy as np

@dataclass
class SignalResult:
    model: str
    netuid: int
    score: float        # -10 to +10
    confidence: float   # 0.0 to 1.0
    reason: str
    meta: dict = field(default_factory=dict)

def emission_price_divergence(netuid: int, lookback_cycles: int = 3) -> SignalResult:
    rows = store.get_subnet_history(netuid, days=7)
    rows = [r for r in rows if r.get("alpha_price_tao") and r.get("total_emission_tao")]
    if len(rows) < lookback_cycles + 1:
        return SignalResult(
            model="emission_price_divergence", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    recent = rows[-(lookback_cycles + 1):]

    price_change = (recent[-1]["alpha_price_tao"] - recent[0]["alpha_price_tao"]) / abs(recent[0]["alpha_price_tao"])
    emission_change = (recent[-1]["total_emission_tao"] - recent[0]["total_emission_tao"]) / abs(recent[0]["total_emission_tao"])

    price_up = price_change > 0.02
    price_down = price_change < -0.02
    emission_up = emission_change > 0.02
    emission_down = emission_change < -0.02

    if emission_up and price_up:
        return SignalResult(
            model="emission_price_divergence", netuid=netuid,
            score=8, confidence=0.9,
            reason=f"emission +{emission_change:.1%} and price +{price_change:.1%} aligned",
            meta={"price_change": price_change, "emission_change": emission_change}
        )
    elif emission_down and price_down:
        return SignalResult(
            model="emission_price_divergence", netuid=netuid,
            score=-8, confidence=0.9,
            reason=f"emission {emission_change:.1%} and price {price_change:.1%} both declining",
            meta={"price_change": price_change, "emission_change": emission_change}
        )
    elif emission_up and price_down:
        return SignalResult(
            model="emission_price_divergence", netuid=netuid,
            score=-7, confidence=0.85,
            reason=f"divergence: emission +{emission_change:.1%} but price {price_change:.1%}",
            meta={"price_change": price_change, "emission_change": emission_change}
        )
    elif emission_down and price_up:
        return SignalResult(
            model="emission_price_divergence", netuid=netuid,
            score=2, confidence=0.6,
            reason=f"price leading emission — wait for confirmation",
            meta={"price_change": price_change, "emission_change": emission_change}
        )
    else:
        return SignalResult(
            model="emission_price_divergence", netuid=netuid,
            score=0, confidence=0.5,
            reason=f"no clear move (emission {emission_change:.1%}, price {price_change:.1%})",
            meta={"price_change": price_change, "emission_change": emission_change}
        )
    
def alpha_price_momentum(netuid: int, fast_cycles: int = 2, slow_cycles: int = 6):
    rows = store.get_alpha_price_history(netuid, days = 14)

    if len(rows) < slow_cycles + 1:
        return SignalResult(
            model="alpha_price_momentum", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    latest = rows[-1]["alpha_price_tao"]
    fast_baseline = rows[-(fast_cycles+1)]["alpha_price_tao"]
    slow_baseline = rows[-(slow_cycles+1)]["alpha_price_tao"]

    fast_mom = (latest - fast_baseline) / abs(fast_baseline)
    slow_mom = (latest - slow_baseline) / abs(slow_baseline)

    acceleration = fast_mom - slow_mom

    if fast_mom > 0.05 and acceleration > 0:
        return SignalResult(
            model="alpha_price_momentum", netuid=netuid,
            score=8, confidence=0.85,
            reason=f"strong momentum: fast +{fast_mom:.1%}, accelerating",
            meta={"fast_mom": fast_mom, "slow_mom": slow_mom, "acceleration": acceleration}
        )
    elif fast_mom > 0.02:
        return SignalResult(
            model="alpha_price_momentum", netuid=netuid,
            score=4, confidence=0.65,
            reason=f"mild upward momentum: +{fast_mom:.1%}",
            meta={"fast_mom": fast_mom, "slow_mom": slow_mom, "acceleration": acceleration}
        )
    elif fast_mom < -0.05 and acceleration < 0:
        return SignalResult(
            model="alpha_price_momentum", netuid=netuid,
            score=-8, confidence=0.85,
            reason=f"strong downward momentum: {fast_mom:.1%}, accelerating",
            meta={"fast_mom": fast_mom, "slow_mom": slow_mom, "acceleration": acceleration}
        )
    elif fast_mom < -0.02:
        return SignalResult(
            model="alpha_price_momentum", netuid=netuid,
            score=-4, confidence=0.65,
            reason=f"mild downward momentum: {fast_mom:.1%}",
            meta={"fast_mom": fast_mom, "slow_mom": slow_mom, "acceleration": acceleration}
        )
    else:
        return SignalResult(
            model="alpha_price_momentum", netuid=netuid,
            score=0, confidence=0.5,
            reason=f"flat price action (fast {fast_mom:.1%}, slow {slow_mom:.1%})",
            meta={"fast_mom": fast_mom, "slow_mom": slow_mom, "acceleration": acceleration}
        )

def zscore_anomaly(netuid: int, days: int = 14) -> SignalResult:
    rows = store.get_subnet_history(netuid, days=days)
    rows = [r for r in rows if r.get("total_emission_tao") is not None]
    if len(rows) < 6:
        return SignalResult(
            model="zscore_anomaly", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    emissions = [r["total_emission_tao"] for r in rows]
    mean = sum(emissions) / len(emissions)
    variance = sum((x - mean) ** 2 for x in emissions) / len(emissions)
    std = variance ** 0.5

    if std == 0:
        return SignalResult(
            model="zscore_anomaly", netuid=netuid,
            score=0, confidence=0.5, reason="no variation in emissions"
        )

    z = (emissions[-1] - mean) / std

    if z > 2.5:
        return SignalResult(
            model="zscore_anomaly", netuid=netuid,
            score=-5, confidence=0.85,
            reason=f"emission spike (z={z:.2f}) — anomaly block, hold only",
            meta={"z_score": z, "mean": mean, "std": std}
        )
    elif z > 1.5:
        return SignalResult(
            model="zscore_anomaly", netuid=netuid,
            score=2, confidence=0.7,
            reason=f"above average emission (z={z:.2f})",
            meta={"z_score": z, "mean": mean, "std": std}
        )
    elif z < -2.5:
        return SignalResult(
            model="zscore_anomaly", netuid=netuid,
            score=-7, confidence=0.85,
            reason=f"emission crash (z={z:.2f}) — strong exit signal",
            meta={"z_score": z, "mean": mean, "std": std}
        )
    elif z < -1.5:
        return SignalResult(
            model="zscore_anomaly", netuid=netuid,
            score=-3, confidence=0.7,
            reason=f"below average emission (z={z:.2f})",
            meta={"z_score": z, "mean": mean, "std": std}
        )
    else:
        return SignalResult(
            model="zscore_anomaly", netuid=netuid,
            score=1, confidence=0.6,
            reason=f"normal emission range (z={z:.2f})",
            meta={"z_score": z, "mean": mean, "std": std}
        )
    

def regime_detection(netuid: int, days: int = 7):
    rows = store.get_subnet_history(netuid, days=days)
    rows = [r for r in rows if r.get("total_stake_tao") and r.get("total_emission_tao")]
    if len(rows) < 4:
        return SignalResult(
            model="regime_detection", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    mid = len(rows) // 2
    early = rows[:mid]
    late = rows[mid:]
    avg = lambda lst, key: sum(r[key] for r in lst) / len(lst)

    early_price = avg(early, "alpha_price_tao")
    late_price = avg(late, "alpha_price_tao")

    early_emission = avg(early, "total_emission_tao")
    late_emission = avg(late, "total_emission_tao") 

    early_stake = avg(early, "total_stake_tao")
    late_stake = avg(late, "total_stake_tao")

    price_up      = late_price    > early_price    * 1.02
    price_down    = late_price    < early_price    * 0.98
    emission_up   = late_emission > early_emission * 1.02
    emission_down = late_emission < early_emission * 0.98
    stake_up      = late_stake    > early_stake    * 1.02
    stake_down    = late_stake    < early_stake    * 0.98

    ups   = sum([price_up, emission_up, stake_up])
    downs = sum([price_down, emission_down, stake_down])

    if ups >= 2 and downs == 0:
        regime, score, confidence = "growth", 6, 0.8
        reason = f"growth regime: {ups}/3 metrics trending up"
    elif ups >= 2 and downs == 1:
        regime, score, confidence = "growth", 4, 0.65
        reason = f"likely growth: {ups}/3 up, minor weakness in one metric"
    elif downs >= 2 and ups == 0:
        regime, score, confidence = "declining", -6, 0.8
        reason = f"declining regime: {downs}/3 metrics trending down"
    elif downs >= 2 and ups == 1:
        regime, score, confidence = "declining", -4, 0.65
        reason = f"likely declining: {downs}/3 down, one metric holding"
    else:
        regime, score, confidence = "stable", 0, 0.5
        reason = f"stable/mixed regime: {ups} up, {downs} down"

    return SignalResult(
        model="regime_detection", netuid=netuid,
        score=score, confidence=confidence, reason=reason,
        meta={"regime": regime, "price_up": price_up, "emission_up": emission_up,
              "stake_up": stake_up, "ups": ups, "downs": downs}
    )

def death_spiral_warning(netuid: int, days: int=7):
    churn_rows = store.get_churn_history(netuid, days=days) 
    subnet_rows = store.get_subnet_history(netuid, days=days)
    subnet_rows = [r for r in subnet_rows if r.get("total_stake_tao") and r.get("total_emission_tao")]

    if len(churn_rows) < 2 or len(subnet_rows) < 3:
        return SignalResult(
            model="death_spiral_warning", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    churn_rates  = [r["churn_rate"]         for r in churn_rows if r.get("churn_rate") is not None]
    dereg_counts = [r["deregistered_count"] for r in churn_rows if r.get("deregistered_count") is not None]

    mid             = len(churn_rates) // 2
    avg_early_churn = sum(churn_rates[:mid]) / max(len(churn_rates[:mid]), 1)
    avg_late_churn  = sum(churn_rates[mid:]) / max(len(churn_rates[mid:]), 1)

    # only flag acceleration if driven by deregistrations, not new registrations
    mid_d           = len(dereg_counts) // 2
    avg_early_dereg = sum(dereg_counts[:mid_d]) / max(len(dereg_counts[:mid_d]), 1)
    avg_late_dereg  = sum(dereg_counts[mid_d:]) / max(len(dereg_counts[mid_d:]), 1)
    dereg_accelerating = avg_late_dereg > avg_early_dereg * 1.5 and avg_late_dereg > 2

    churn_accelerating = avg_late_churn > avg_early_churn * 1.5 and avg_late_churn > 0.05 and dereg_accelerating

    emissions = [r["total_emission_tao"] for r in subnet_rows]
    stakes    = [r["total_stake_tao"]    for r in subnet_rows]
    emission_change = (emissions[-1] - emissions[0]) / abs(emissions[0]) if emissions[0] != 0 else 0
    stake_change    = (stakes[-1]    - stakes[0])    / abs(stakes[0])    if stakes[0]    != 0 else 0

    latest_churn     = churn_rates[-1] if churn_rates else 0
    emission_falling = emission_change < -0.05
    stake_falling    = stake_change    < -0.05

    if churn_accelerating and emission_falling and stake_falling:
        return SignalResult(
            model="death_spiral_warning", netuid=netuid,
            score=-9, confidence=0.9,
            reason=f"full spiral: churn {avg_early_churn:.1%}→{avg_late_churn:.1%}, emission {emission_change:.1%}, stake {stake_change:.1%}",
            meta={"churn_accelerating": True, "avg_late_churn": avg_late_churn,
                  "emission_change": emission_change, "stake_change": stake_change}
        )
    elif churn_accelerating and (emission_falling or stake_falling):
        return SignalResult(
            model="death_spiral_warning", netuid=netuid,
            score=-7, confidence=0.8,
            reason=f"early spiral: churn accelerating + {'emission' if emission_falling else 'stake'} falling",
            meta={"churn_accelerating": True, "avg_late_churn": avg_late_churn,
                  "emission_change": emission_change, "stake_change": stake_change}
        )
    elif churn_accelerating:
        return SignalResult(
            model="death_spiral_warning", netuid=netuid,
            score=-5, confidence=0.7,
            reason=f"churn accelerating ({avg_early_churn:.1%}→{avg_late_churn:.1%}) — monitor closely",
            meta={"churn_accelerating": True, "avg_late_churn": avg_late_churn,
                  "emission_change": emission_change, "stake_change": stake_change}
        )
    elif latest_churn > 0.10 and dereg_counts and dereg_counts[-1] > avg_early_dereg:
        return SignalResult(
            model="death_spiral_warning", netuid=netuid,
            score=-3, confidence=0.6,
            reason=f"elevated deregistrations ({latest_churn:.1%} churn) but not accelerating yet",
            meta={"churn_accelerating": False, "avg_late_churn": avg_late_churn,
                  "emission_change": emission_change, "stake_change": stake_change}
        )
    else:
        return SignalResult(
            model="death_spiral_warning", netuid=netuid,
            score=2, confidence=0.6,
            reason=f"stable: churn {latest_churn:.1%}, emission {emission_change:.1%}, stake {stake_change:.1%}",
            meta={"churn_accelerating": False, "avg_late_churn": avg_late_churn,
                  "emission_change": emission_change, "stake_change": stake_change}
        )

def stake_concentration_gini(netuid: int):
    stakes = store.get_latest_validator_stakes(netuid)
    values = sorted([v for v in stakes.values() if v is not None and v>0])
    if len(values) < 3:
        return SignalResult(
            model="stake_concentration_gini", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    n = len(values)
    total = sum(values)
    gini = sum((2*(i+1)-n-1)* v for i, v in enumerate(values)) / (n * total) if total > 0 else 0

    top3_share = sum(values[-3:]) / total

    if gini > 0.85:
        return SignalResult(
            model="stake_concentration_gini", netuid=netuid,
            score=-8, confidence=0.9,
            reason=f"extreme concentration (gini={gini:.2f}, top3={top3_share:.0%}) — block entry",
            meta={"gini": gini, "top3_share": top3_share, "validator_count": n}
        )
    elif gini > 0.70:
        return SignalResult(
            model="stake_concentration_gini", netuid=netuid,
            score=-5, confidence=0.75,
            reason=f"high concentration (gini={gini:.2f}, top3={top3_share:.0%})",
            meta={"gini": gini, "top3_share": top3_share, "validator_count": n}
        )
    elif gini > 0.50:
        return SignalResult(
            model="stake_concentration_gini", netuid=netuid,
            score=-2, confidence=0.6,
            reason=f"moderate concentration (gini={gini:.2f})",
            meta={"gini": gini, "top3_share": top3_share, "validator_count": n}
        )
    else:
        return SignalResult(
            model="stake_concentration_gini", netuid=netuid,
            score=2, confidence=0.65,
            reason=f"healthy distribution (gini={gini:.2f}, top3={top3_share:.0%})",
            meta={"gini": gini, "top3_share": top3_share, "validator_count": n}
        )
    

def kalman_filter_price(netuid: int, days: int=30):
    rows = store.get_alpha_price_history(netuid, days=days)
    prices = [r["alpha_price_tao"] for r in rows if r.get("alpha_price_tao") is not None]
    if len(prices) < 10:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    
    mean = sum(prices) / len(prices)
    R = sum((p - mean) ** 2 for p in prices) / len(prices) #measurement noise- how noisy price readings are
    Q = R * 0.1 #process noise #how much we expect true price to move each cycle
    #R is how much we trust price radings and Q is how much true price moves
    #higher variance means prices jump a lot so we trust it less and if low then we trust them more
    #Q is how much we expect the true price to move
    x = prices[0]   
    p = R
    #p is uncertainty about that belief  
    innovations = []
    for price in prices[1:]:
        p = p + Q #reflects uncertainty after 4 hours
        K = p / (p + R) #kalman gain, close to 1- trust new reading vice versa
        innovation = price - x
        x = x + K * innovation
        p = (1 - K) * p
        innovations.append(innovation)
    recent_innovations = innovations[-3:] if innovations else []
    avg_innovation = sum(recent_innovations) / len(recent_innovations) if recent_innovations else 0
    latest_innovation = recent_innovations[-1] if recent_innovations else 0
    std = (sum(i**2 for i in innovations) / len(innovations)) ** 0.5 if innovations else 0

    if std == 0:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=0, confidence=0.5, reason="no variation in price"
        )

    normalised = avg_innovation / std

    if abs(latest_innovation) > 3 * std:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=-5, confidence=0.85,
            reason=f"anomalous price spike (innovation={latest_innovation:.4f}, {abs(latest_innovation)/std:.1f}x std)",
            meta={"kalman_price": x, "latest_innovation": latest_innovation, "std": std}
        )
    elif normalised > 1.5:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=7, confidence=0.8,
            reason=f"price consistently above kalman estimate — real upward pressure",
            meta={"kalman_price": x, "avg_innovation": avg_innovation, "std": std}
        )
    elif normalised > 0.5:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=3, confidence=0.65,
            reason=f"mild positive deviation from kalman estimate",
            meta={"kalman_price": x, "avg_innovation": avg_innovation, "std": std}
        )
    elif normalised < -1.5:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=-7, confidence=0.8,
            reason=f"price consistently below kalman estimate — real downward pressure",
            meta={"kalman_price": x, "avg_innovation": avg_innovation, "std": std}
        )
    elif normalised < -0.5:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=-3, confidence=0.65,
            reason=f"mild negative deviation from kalman estimate",
            meta={"kalman_price": x, "avg_innovation": avg_innovation, "std": std}
        )
    else:
        return SignalResult(
            model="kalman_filter_price", netuid=netuid,
            score=0, confidence=0.5,
            reason=f"price tracking kalman estimate closely — no clear signal",
            meta={"kalman_price": x, "avg_innovation": avg_innovation, "std": std}
        )

def registration_cost_velocity(netuid: int, days: int = 7):
    rows = store.get_subnet_history(netuid, days=days)
    rows = [r for r in rows if r.get("reg_cost_tao") is not None]
    if len(rows) < 4:
        return SignalResult(
            model="registration_cost_velocity", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    mid = len(rows) // 2
    early_avg = sum(r["reg_cost_tao"] for r in rows[:mid]) / mid
    late_avg  = sum(r["reg_cost_tao"] for r in rows[mid:]) / (len(rows) - mid)

    change = (late_avg - early_avg) / abs(early_avg) if early_avg != 0 else 0

    if change > 0.20:
        return SignalResult(
            model="registration_cost_velocity", netuid=netuid,
            score=7, confidence=0.85,
            reason=f"reg cost surging +{change:.1%} — strong miner demand incoming",
            meta={"early_avg": early_avg, "late_avg": late_avg, "change": change}
        )
    elif change > 0.08:
        return SignalResult(
            model="registration_cost_velocity", netuid=netuid,
            score=3, confidence=0.65,
            reason=f"reg cost rising +{change:.1%} — mild demand increase",
            meta={"early_avg": early_avg, "late_avg": late_avg, "change": change}
        )
    elif change < -0.20:
        return SignalResult(
            model="registration_cost_velocity", netuid=netuid,
            score=-7, confidence=0.85,
            reason=f"reg cost collapsing {change:.1%} — miners leaving, demand drying up",
            meta={"early_avg": early_avg, "late_avg": late_avg, "change": change}
        )
    elif change < -0.08:
        return SignalResult(
            model="registration_cost_velocity", netuid=netuid,
            score=-3, confidence=0.65,
            reason=f"reg cost declining {change:.1%} — softening demand",
            meta={"early_avg": early_avg, "late_avg": late_avg, "change": change}
        )
    else:
        return SignalResult(
            model="registration_cost_velocity", netuid=netuid,
            score=0, confidence=0.5,
            reason=f"reg cost stable ({change:.1%} change)",
            meta={"early_avg": early_avg, "late_avg": late_avg, "change": change}
        )

def isolation_forest_anomaly(netuid: int, days: int = 30):
    rows = store.get_subnet_history(netuid, days=days)
    rows = [r for r in rows if all(r.get(k) is not None for k in ["total_emission_tao", "alpha_price_tao", "total_stake_tao", "reg_cost_tao"])]
    if len(rows) < 20:
        return SignalResult(
            model="isolation_forest_anomaly", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    features = [[r["total_emission_tao"], r["alpha_price_tao"], r["total_stake_tao"], r["reg_cost_tao"]] for r in rows]

    model = IsolationForest(contamination=0.1, random_state=42)
    model.fit(features)
    scores = model.decision_function(features)
    latest_score = scores[-1]
    avg_recent = sum(scores[-3:]) / 3

    if latest_score < -0.15 and avg_recent < -0.10:
        return SignalResult(
            model="isolation_forest_anomaly", netuid=netuid,
            score=-8, confidence=0.9,
            reason=f"strong multi-metric anomaly detected (score={latest_score:.3f}) — block entry",
            meta={"isolation_score": latest_score, "avg_recent": avg_recent}
        )
    elif latest_score < -0.05:
        return SignalResult(
            model="isolation_forest_anomaly", netuid=netuid,
            score=-4, confidence=0.7,
            reason=f"mild anomaly in combined metrics (score={latest_score:.3f}) — caution",
            meta={"isolation_score": latest_score, "avg_recent": avg_recent}
        )
    else:
        return SignalResult(
            model="isolation_forest_anomaly", netuid=netuid,
            score=1, confidence=0.6,
            reason=f"no multi-metric anomaly detected (score={latest_score:.3f})",
            meta={"isolation_score": latest_score, "avg_recent": avg_recent}
        )


def monte_carlo_emission(netuid: int, days: int = 30, simulations: int = 100, cycles: int = 6):
    rows = store.get_subnet_history(netuid, days=days)
    rows = [r for r in rows if r.get("total_emission_tao") is not None]
    if len(rows) < 8:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    emissions = [r["total_emission_tao"] for r in rows]
    changes = [(emissions[i] - emissions[i-1]) / abs(emissions[i-1]) for i in range(1, len(emissions)) if emissions[i-1] != 0]
    if len(changes) < 4:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=0, confidence=0.5, reason="not enough emission change data"
        )

    mean = sum(changes) / len(changes)
    std = (sum((c - mean) ** 2 for c in changes) / len(changes)) ** 0.5

    if std == 0:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=0, confidence=0.5, reason="no variation in emission changes"
        )

    current = emissions[-1]
    final_values = []
    for _ in range(simulations):
        value = current
        for _ in range(cycles):
            change = random.gauss(mean, std)
            value *= (1 + change)
        final_values.append(value)

    prob_up   = sum(1 for v in final_values if v > current * 1.10) / simulations
    prob_down = sum(1 for v in final_values if v < current * 0.80) / simulations
    confidence = min(0.5 + len(rows) / 100, 0.85)

    if prob_up > 0.60:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=6, confidence=confidence,
            reason=f"MC: {prob_up:.0%} chance emission up 10%+ in {cycles} cycles",
            meta={"prob_up": prob_up, "prob_down": prob_down, "mean_change": mean, "std": std}
        )
    elif prob_up > 0.40:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=3, confidence=confidence,
            reason=f"MC: {prob_up:.0%} chance emission up 10%+ in {cycles} cycles",
            meta={"prob_up": prob_up, "prob_down": prob_down, "mean_change": mean, "std": std}
        )
    elif prob_down > 0.60:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=-7, confidence=confidence,
            reason=f"MC: {prob_down:.0%} chance emission down 20%+ in {cycles} cycles",
            meta={"prob_up": prob_up, "prob_down": prob_down, "mean_change": mean, "std": std}
        )
    elif prob_down > 0.40:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=-4, confidence=confidence,
            reason=f"MC: {prob_down:.0%} chance emission down 20%+ in {cycles} cycles",
            meta={"prob_up": prob_up, "prob_down": prob_down, "mean_change": mean, "std": std}
        )
    else:
        return SignalResult(
            model="monte_carlo_emission", netuid=netuid,
            score=0, confidence=confidence,
            reason=f"MC: no dominant scenario (up {prob_up:.0%}, down {prob_down:.0%})",
            meta={"prob_up": prob_up, "prob_down": prob_down, "mean_change": mean, "std": std}
        )
    
def hmm_regime_detection(netuid: int, days: int = 30):
    rows = store.get_subnet_history(netuid, days=days)
    rows = [r for r in rows if r.get("alpha_price_tao") is not None and r.get("total_emission_tao") is not None and r.get("total_stake_tao") is not None]
    if len(rows) < 30:
        return SignalResult(
            model="hmm_regime_detection", netuid=netuid,
            score=0, confidence=0, reason="insufficient data"
        )
    features = []
    for i in range(1, len(rows)):
        price_change = (rows[i]["alpha_price_tao"] - rows[i-1]["alpha_price_tao"]) / abs(rows[i-1]["alpha_price_tao"])
        emission_change = (rows[i]["total_emission_tao"] - rows[i-1]["total_emission_tao"]) / abs(rows[i-1]["total_emission_tao"])
        stake_change = (rows[i]["total_stake_tao"] - rows[i-1]["total_stake_tao"]) / abs(rows[i-1]["total_stake_tao"])
        features.append([price_change, emission_change, stake_change])

    X = np.array(features)

    model = GaussianHMM(n_components=3, covariance_type="diag", n_iter=100, random_state=42)
    model.fit(X)
    states = model.predict(X)
    current_state = states[-1]

    state_emission_means = []
    for s in range(3):
        rows_in_state = X[states == s, 1]
        if len(rows_in_state) > 0:
            state_emission_means.append(rows_in_state.mean())
        else:
            state_emission_means.append(0)
    bullish_state = int(np.argmax(state_emission_means))
    bearish_state = int(np.argmin(state_emission_means))

    if current_state == bullish_state:
        score, confidence, label = 7, 0.75, "bullish"
    elif current_state == bearish_state:
        score, confidence, label = -7, 0.75, "bearish"
    else:
        score, confidence, label = 0, 0.5, "neutral"

    return SignalResult(
        model="hmm_regime_detection", netuid=netuid,
        score=score, confidence=confidence,
        reason=f"HMM regime detection: current state {current_state} classified as {label}",
        meta={"current_state": current_state, "state_emission_means": state_emission_means}
    )
    


