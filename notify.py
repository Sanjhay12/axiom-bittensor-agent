import httpx
import os
import logging

logger = logging.getLogger(__name__)

_chat_id: str | None = None


def set_chat_id(cid: int | str):
    global _chat_id
    _chat_id = str(cid)


async def send(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    cid = _chat_id or os.getenv("OWNER_CHAT_ID")
    if not token or not cid:
        logger.warning("notify.send: no token or chat_id configured")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": cid, "text": text},
            )
    except Exception as e:
        logger.warning(f"Telegram notify failed: {e}")
