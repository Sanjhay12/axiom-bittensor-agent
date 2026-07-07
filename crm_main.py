"""Entry point for the Cedar Ridge CRM agent (Render worker)."""
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


def _boot_diagnostics():
    """Print what THIS running instance actually sees — commit + owners + code-change
    config — so a stale/duplicate service or unset env var is obvious in the deploy log."""
    import subprocess
    import crm_mail
    import crm_coder
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
        ).stdout.strip() or "unknown"
    except Exception:
        commit = "unknown"
    sec = crm_coder.CODE_SECRET or ""
    logging.getLogger("crm_main").info(
        "crm boot: commit=%s | owners=%s | code_changes=%s (secret_len=%d clean=%s) | github_token=%s",
        commit,
        crm_mail.OWNER_EMAILS or "(none!)",
        "ENABLED" if crm_coder.CODE_SECRET else "DISABLED (CRM_CODE_SECRET not set)",
        len(sec),
        sec == sec.strip() and sec != "",
        "set" if crm_coder.GITHUB_TOKEN else "MISSING",
    )


async def main():
    _boot_diagnostics()
    crm_store.init_crm_db()
    await asyncio.gather(
        crm_agent.run_loop(),
        crm_radar.run_daily_loop(),
        crm_outbound.run_daily_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
