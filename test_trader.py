import trader
from dotenv import load_dotenv                                                      
import store     
import asyncio         
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]%(message)s")
load_dotenv()
store.init_db()

trader.init_db()
#print("Tables created")
#asyncio.run(trader._run_cycle())

import analytics
                                                                                      
for netuid in [1, 5, 18, 64]:                             
    print(f"\n--- SN{netuid} ---")
    signals = [
          analytics.emission_price_divergence(netuid),
          analytics.alpha_price_momentum(netuid),
          analytics.zscore_anomaly(netuid),
          analytics.regime_detection(netuid),
          analytics.death_spiral_warning(netuid),
          analytics.stake_concentration_gini(netuid),
          analytics.kalman_filter_price(netuid),
          analytics.registration_cost_velocity(netuid),
      ]
    for s in signals:
        print(f"  {s.model}: score={s.score}, confidence={s.confidence},reason={s.reason}")
