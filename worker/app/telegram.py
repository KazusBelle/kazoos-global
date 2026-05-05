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


async def send_telegram_photo(
    token: str, chat_id: str, photo: bytes, caption: str
) -> bool:
    if not token or not chat_id:
        logger.info("telegram not configured — skipping photo alert: %s", caption)
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"photo": ("setup.png", photo, "image/png")},
            )
            r.raise_for_status()
            return True
    except Exception as exc:
        logger.warning("telegram photo send failed: %s", exc)
        return False


async def send_telegram_media_group(
    token: str, chat_id: str, photos: list[bytes], caption: str
) -> bool:
    """Send several photos as a single Telegram album. Caption is attached
    to the first photo (Telegram renders it under the album)."""
    if not token or not chat_id:
        logger.info("telegram not configured — skipping media-group: %s", caption)
        return False
    if not photos:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMediaGroup"
    media = []
    files = {}
    for idx, photo in enumerate(photos):
        attach_name = f"photo_{idx}"
        item = {"type": "photo", "media": f"attach://{attach_name}"}
        if idx == 0:
            item["caption"] = caption
            item["parse_mode"] = "HTML"
        media.append(item)
        files[attach_name] = (f"{attach_name}.png", photo, "image/png")
    try:
        import json as _json
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url,
                data={"chat_id": chat_id, "media": _json.dumps(media)},
                files=files,
            )
            r.raise_for_status()
            return True
    except Exception as exc:
        logger.warning("telegram media-group send failed: %s", exc)
        return False
