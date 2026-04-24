"""
Worker entry-point.

Responsibilities:
- Periodically (every REFRESH_INTERVAL_SEC) pull the list of active coins
  from Postgres.
- For each coin fetch D1 and H1 klines from Binance Futures and compute
  the KazusGlobal (D1) + KazusLocal (H1) snapshot.
- Upsert the result into `snapshots`.
- Update per-(symbol, timeframe) alert state; send a Telegram notification
  only on OTE re-entry (outside → inside). Do NOT resend while the price
  stays inside OTE. When price leaves OTE the state resets.
- Update `system_status.last_refresh_at` and `last_error`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from kazus_logic.binance import BinanceFuturesClient
from kazus_logic.compute import SymbolSnapshot, compute_symbol
from kazus_db.models import AlertState, Coin, Snapshot, SystemStatus

from .db import SessionLocal
from .settings import get_settings
from .telegram import send_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("kazus.worker")


async def run_once(client: BinanceFuturesClient, settings) -> None:
    with SessionLocal() as db:
        coins: list[str] = [
            c.symbol
            for c in db.query(Coin).filter(Coin.is_active.is_(True)).order_by(Coin.symbol.asc()).all()
        ]

    if not coins:
        logger.info("no active coins — skipping cycle")
        _touch_status(None)
        return

    alert_timeframes = {
        t.strip() for t in settings.alert_timeframes.split(",") if t.strip()
    }

    last_error: str | None = None
    for symbol in coins:
        try:
            snap = await compute_symbol(
                client, symbol,
                d1_limit=settings.d1_bar_limit,
                h1_limit=settings.h1_bar_limit,
            )
        except Exception as exc:
            logger.warning("compute failed for %s: %s", symbol, exc)
            last_error = f"{symbol}: {exc}"
            continue

        with SessionLocal() as db:
            _upsert_snapshots(db, snap)
            alerts_to_send = _update_alert_states(db, snap, alert_timeframes)
            db.commit()

        for tf, text in alerts_to_send:
            await send_telegram(settings.telegram_bot_token, settings.telegram_chat_id, text)
            with SessionLocal() as db:
                row = (
                    db.query(AlertState)
                    .filter(AlertState.symbol == symbol, AlertState.timeframe == tf)
                    .first()
                )
                if row is not None:
                    row.last_alert_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    db.commit()

    _touch_status(last_error)


def _upsert_snapshots(db: Session, snap: SymbolSnapshot) -> None:
    for tf, result, trend in (
        ("D1", snap.global_result, snap.global_trend),
        ("H1", snap.local_result, snap.local_trend),
    ):
        row = (
            db.query(Snapshot)
            .filter(Snapshot.symbol == snap.symbol, Snapshot.timeframe == tf)
            .first()
        )
        if row is None:
            row = Snapshot(symbol=snap.symbol, timeframe=tf)
            db.add(row)
        row.price = snap.price
        row.direction = result.direction
        row.zone = result.zone
        row.in_ote = result.in_ote
        row.setup = result.setup
        row.retracement = result.retracement
        row.fib_low = result.fib_low
        row.fib_high = result.fib_high
        row.ote_low_price = result.ote_low_price
        row.ote_high_price = result.ote_high_price
        row.trend = trend
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def _update_alert_states(
    db: Session, snap: SymbolSnapshot, timeframes: Iterable[str]
) -> list[tuple[str, str]]:
    """Return list of (timeframe, message) pairs that still need to be sent."""
    out: list[tuple[str, str]] = []
    for tf, result in (("D1", snap.global_result), ("H1", snap.local_result)):
        if tf not in timeframes:
            continue

        row = (
            db.query(AlertState)
            .filter(AlertState.symbol == snap.symbol, AlertState.timeframe == tf)
            .first()
        )
        if row is None:
            row = AlertState(symbol=snap.symbol, timeframe=tf, in_ote=False)
            db.add(row)
            db.flush()

        prev_in_ote = bool(row.in_ote)
        now_in_ote = bool(result.in_ote)

        # Entering OTE → send exactly one alert.
        if now_in_ote and not prev_in_ote:
            out.append(
                (
                    tf,
                    _format_alert(snap.symbol, tf, snap.price, result),
                )
            )

        row.in_ote = now_in_ote
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return out


def _format_alert(symbol: str, timeframe: str, price: float, result) -> str:
    tf_label = "D1 (GLOBAL)" if timeframe == "D1" else "H1 (LOCAL)"
    dir_label = result.direction.upper()
    ret_pct = f"{result.retracement * 100:.2f}%" if result.retracement is not None else "n/a"
    zone_label = _format_ote_range(result)
    return (
        f"<b>OTE entry</b> — {symbol}\n"
        f"TF: {tf_label}\n"
        f"Direction: {dir_label}\n"
        f"Price: {price}\n"
        f"Retracement: {ret_pct}\n"
        f"{zone_label}"
    )


def _format_ote_range(result) -> str:
    if result.ote_low_price is None or result.ote_high_price is None:
        return ""
    return f"OTE zone: {result.ote_low_price:.6g} – {result.ote_high_price:.6g}"


def _touch_status(last_error: str | None) -> None:
    with SessionLocal() as db:
        row = db.query(SystemStatus).filter(SystemStatus.id == 1).first()
        if row is None:
            row = SystemStatus(id=1)
            db.add(row)
        row.last_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.last_error = last_error[:500] if last_error else None
        db.commit()


async def main() -> None:
    settings = get_settings()
    logger.info("worker starting; refresh every %ss", settings.refresh_interval_sec)

    stop_event = asyncio.Event()

    def _stop(*_):
        logger.info("stop signal received")
        stop_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _stop)
    except NotImplementedError:
        # Windows fallback, though we only target linux
        pass

    client = BinanceFuturesClient()
    try:
        while not stop_event.is_set():
            try:
                await run_once(client, settings)
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=settings.refresh_interval_sec
                )
            except asyncio.TimeoutError:
                continue
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
