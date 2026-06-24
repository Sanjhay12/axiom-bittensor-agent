MAX_POSITION_SIZE = 0.10      # max 10% of portfolio per subnet
MIN_POSITION_SIZE = 0.05      # min 5% of portfolio per subnet
MAX_TOTAL_DEPLOYED = 0.80     # max 80% of portfolio deployed at once
MAX_OPEN_POSITIONS = 10       # max number of concurrent positions                  
MIN_TAO_BALANCE = 10.0        # always keep this much TAO free                      
   
ENTRY_SCORE_THRESHOLD = 5.0    # min score to consider entry (backtest-validated; raised back from 2.0 — the lowered threshold let in low-conviction entries that mostly got trailing-stopped out)
ENTRY_CYCLES_REQUIRED = 1   # consecutive cycles above threshold before entering
EXIT_SCORE_THRESHOLD = -3    # exit immediately if score drops below this

STOP_LOSS = 0.15              # exit if position down 15%
TAKE_PROFIT = 10.0            # effectively disabled (backtest-validated) — capping at 20% guaranteed missing moonshot-style winners
TRAILING_STOP = 0.10          # trail by 10% from peak

REPLACEMENT_MARGIN = 0.75     # new candidate must beat the weakest open position's score by this much to trigger a swap (avoids churning on score noise)

COOLDOWN_CYCLES = 6           # cycles to wait before re-entering same subnet
NEW_SUBNET_MIN_DAYS = 7       # don't enter subnets with less than 7 days data

PORTFOLIO_SIZE_TAO = 100.0    # paper trading starting balance

MIN_HOLD_DAYS = 3                 # minimum holding period before considering exit
MAX_HOLD_DAYS = 7                 # exit if position still open after 7 days
EXIT_CYCLES_REQUIRED = 2              # consecutive cycles below threshold before exiting
TIME_EXIT_MIN_PNL = 0.05          # skip time exit if position is up more than this
TIME_EXIT_MAX_DRAWDOWN = 0.05     # skip time exit if still within 5% of peak
EVICT_MIN_PNL = 0.05              # don't evict a position that's up more than 5% even if signal is weak