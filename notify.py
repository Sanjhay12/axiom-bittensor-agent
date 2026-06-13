import httpx
import os
import logging

logger = logging.getLogger(__name__)

_chat_id: str | None = None


def set_chat_id(cid: int | str):
    global _chat_id
    _chat_id = str(cid)
    try:
        import store
        store.set_config("telegram_chat_id", _chat_id)
    except Exception as e:
        logger.warning(f"Failed to persist chat_id: {e}")


def load_chat_id():
    """Restore the last known chat_id from the database (call on startup)."""
    global _chat_id
    if _chat_id:
        return
    try:
        import store
        cid = store.get_config("telegram_chat_id")
        if cid:
            _chat_id = cid
    except Exception as e:
        logger.warning(f"Failed to load chat_id: {e}")


async def send(text: str, parse_mode: str | None = None):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    cid = _chat_id or os.getenv("OWNER_CHAT_ID")
    if not token or not cid:
        logger.warning("notify.send: no token or chat_id configured")
        return
    payload = {"chat_id": cid, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
    except Exception as e:
        logger.warning(f"Telegram notify failed: {e}")
