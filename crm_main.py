"""Entry point for the Cedar Ridge CRM agent on Railway."""
import asyncio
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

import crm_agent
import crm_radar
import crm_outbound
import crm_store


async def main():
    crm_store.init_crm_db()
    await asyncio.gather(
        crm_agent.run_loop(),
        crm_radar.run_daily_loop(),
        crm_outbound.run_daily_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
