MAX_POSITION_SIZE = 0.10      # max 10% of portfolio per subnet
MIN_POSITION_SIZE = 0.05      # min 5% of portfolio per subnet
MAX_TOTAL_DEPLOYED = 0.80     # max 80% of portfolio deployed at once
MAX_OPEN_POSITIONS = 10       # max number of concurrent positions                  
MIN_TAO_BALANCE = 10.0        # always keep this much TAO free                      
   
ENTRY_SCORE_THRESHOLD = 3.0    # live scores top out ~4.0 (avg top 2.5), so backtest's 5.0 blocked ALL entries; 3.0 = top ~19% of live cycles / ~8 subnets/wk — selective without the churn 2.0 caused
ENTRY_CYCLES_REQUIRED = 1   # consecutive cycles above threshold before entering
EXIT_SCORE_THRESHOLD = -1.0  # exit when score weakens meaningfully (raised from -3 — signal exits average +0.15 TAO)

STOP_LOSS = 0.15              # exit if position down 15% from entry
TAKE_PROFIT = 10.0            # effectively disabled — let winners run
TRAILING_STOP = 0.10          # trail by 10% from peak (tightened from 15% — wide stop was producing 7-13% losses)
TRAILING_STOP_ACTIVATE = 0.03 # arm trailing stop once position is up 3% (lowered from 5%)

REPLACEMENT_MARGIN = 1.5      # new candidate must beat weakest position's score by this much (raised from 0.75 to reduce churn)

COOLDOWN_CYCLES = 6           # cycles to wait before re-entering same subnet
NEW_SUBNET_MIN_DAYS = 7       # don't enter subnets with less than 7 days data

PORTFOLIO_SIZE_TAO = 100.0    # paper trading starting balance

MIN_HOLD_DAYS = 3                 # minimum holding period before considering exit
MAX_HOLD_DAYS = 7                 # exit if position still open after 7 days
EXIT_CYCLES_REQUIRED = 2              # consecutive cycles below threshold before exiting
TIME_EXIT_MIN_PNL = 0.10          # skip time exit if position is up 10%+ — trailing stop handles it from there
EVICT_MIN_PNL = 0.05              # don't evict a position that's up more than 5% even if signal is weak