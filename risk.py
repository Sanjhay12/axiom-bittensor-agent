MAX_POSITION_SIZE = 0.10      # max 10% of portfolio per subnet
MIN_POSITION_SIZE = 0.05      # min 5% of portfolio per subnet
MAX_TOTAL_DEPLOYED = 0.80     # max 80% of portfolio deployed at once
MAX_OPEN_POSITIONS = 10       # max number of concurrent positions                  
MIN_TAO_BALANCE = 10.0        # always keep this much TAO free                      
   
ENTRY_SCORE_THRESHOLD = 5.0    # min score to consider entry (backtest-validated; raised back from 2.0 — the lowered threshold let in low-conviction entries that mostly got trailing-stopped out)
ENTRY_CYCLES_REQUIRED = 1   # consecutive cycles above threshold before entering
EXIT_SCORE_THRESHOLD = -3    # exit immediately if score drops below this

STOP_LOSS = 0.15              # exit if position down 15% from entry
TAKE_PROFIT = 10.0            # effectively disabled — let winners run
TRAILING_STOP = 0.15          # trail by 15% from peak (widened from 10% — subnet prices move 10%+ in normal noise)
TRAILING_STOP_ACTIVATE = 0.05 # only start trailing once position is up 5% — before that, only stop_loss catches downside

REPLACEMENT_MARGIN = 1.5      # new candidate must beat weakest position's score by this much (raised from 0.75 to reduce churn)

COOLDOWN_CYCLES = 6           # cycles to wait before re-entering same subnet
NEW_SUBNET_MIN_DAYS = 7       # don't enter subnets with less than 7 days data

PORTFOLIO_SIZE_TAO = 100.0    # paper trading starting balance

MIN_HOLD_DAYS = 3                 # minimum holding period before considering exit
MAX_HOLD_DAYS = 7                 # exit if position still open after 7 days
EXIT_CYCLES_REQUIRED = 2              # consecutive cycles below threshold before exiting
TIME_EXIT_MIN_PNL = 0.10          # skip time exit if position is up 10%+ — trailing stop handles it from there
EVICT_MIN_PNL = 0.05              # don't evict a position that's up more than 5% even if signal is weak