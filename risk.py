MAX_POSITION_SIZE = 0.10      # max 10% of portfolio per subnet
MIN_POSITION_SIZE = 0.05      # min 5% of portfolio per subnet
MAX_TOTAL_DEPLOYED = 0.80     # max 80% of portfolio deployed at once
MAX_OPEN_POSITIONS = 10       # max number of concurrent positions                  
MIN_TAO_BALANCE = 10.0        # always keep this much TAO free                      
   
# Adaptive entry bar: the ensemble's score scale drifts (historical max 5.3, recent ~4.0) and is
# centered near 0, so any fixed threshold is either too loose (churn) or too tight (starvation — 3.0
# left only 1/129 subnets eligible). Instead the bar floats at a percentile of recent scores.
ENTRY_PERCENTILE = 0.95        # bar = 95th pct of last-N-days scores; ~7 subnets eligible now — under the 10-cap, so no forced eviction/churn
ENTRY_LOOKBACK_DAYS = 7        # window for the rolling percentile
ENTRY_SCORE_FLOOR = 0.5        # absolute safety net: never enter below this even if the rolling bar drops lower
ENTRY_SCORE_THRESHOLD = 1.0    # fallback bar used only when score history is empty (cold start)
ENTRY_CYCLES_REQUIRED = 1   # consecutive cycles above threshold before entering
EXIT_SCORE_THRESHOLD = -1.0  # exit when score weakens meaningfully (raised from -3 — signal exits average +0.15 TAO)

STOP_LOSS = 0.15              # exit if position down 15% from entry
TAKE_PROFIT = 10.0            # effectively disabled — let winners run
TRAILING_STOP = 0.08          # trail by 8% from peak
TRAILING_STOP_ACTIVATE = 0.12 # arm trailing stop only once up 12% — MUST exceed TRAILING_STOP so a trailing exit always locks in a gain (peak*0.92 from a +12% peak ≈ +3%). Prior 3% activate < 10% trail made every trailing exit a guaranteed loss (0% win rate, -7.6 TAO across 14 trades)

BREAKEVEN_ACTIVATE = 0.05    # once a position has been up 5%, ratchet the stop to entry so a winner can't round-trip into a loss
BREAKEVEN_FLOOR = 0.0        # breakeven-stop exit level relative to entry (0.0 = entry price)

REPLACEMENT_MARGIN = 1.5      # new candidate must beat weakest position's score by this much (raised from 0.75 to reduce churn)

COOLDOWN_CYCLES = 6           # cycles to wait before re-entering same subnet
NEW_SUBNET_MIN_DAYS = 7       # don't enter subnets with less than 7 days data

PORTFOLIO_SIZE_TAO = 100.0    # paper trading starting balance

MIN_HOLD_DAYS = 3                 # minimum holding period before considering exit
MAX_HOLD_DAYS = 7                 # exit if position still open after 7 days
EXIT_CYCLES_REQUIRED = 2              # consecutive cycles below threshold before exiting
TIME_EXIT_MIN_PNL = 0.10          # skip time exit if position is up 10%+ — trailing stop handles it from there
EVICT_MIN_PNL = 0.05              # don't evict a position that's up more than 5% even if signal is weak