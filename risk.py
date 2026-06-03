MAX_POSITION_SIZE = 0.10      # max 10% of portfolio per subnet                     
MAX_TOTAL_DEPLOYED = 0.80     # max 80% of portfolio deployed at once               
MAX_OPEN_POSITIONS = 10       # max number of concurrent positions                  
MIN_TAO_BALANCE = 10.0        # always keep this much TAO free                      
   
ENTRY_SCORE_THRESHOLD = 2.0    # min score to consider entry                         
ENTRY_CYCLES_REQUIRED = 1   # consecutive cycles above threshold before entering
EXIT_SCORE_THRESHOLD = -3    # exit immediately if score drops below this

STOP_LOSS = 0.15              # exit if position down 15%
TAKE_PROFIT = 0.20            # exit if position up 20%
TRAILING_STOP = 0.10          # trail by 10% from peak

COOLDOWN_CYCLES = 6           # cycles to wait before re-entering same subnet
NEW_SUBNET_MIN_DAYS = 7       # don't enter subnets with less than 7 days data

PORTFOLIO_SIZE_TAO = 100.0    # paper trading starting balance

MIN_HOLD_DAYS = 3                 # minimum holding period before considering exit
MAX_HOLD_DAYS = 7                 # exit if position still open after 7 days
EXIT_CYCLES_REQUIRED = 2              # consecutive cycles below threshold before exiting