import logging

import httpx

logger = logging.getLogger("kazus.worker.telegram")


async def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        logger.info("telegram not configured — skipping alert: %s", text)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            r.raise_for_status()
            return True
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return False
