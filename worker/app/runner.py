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
import json
import logging
import signal
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from kazus_logic.binance import BinanceFuturesClient
from kazus_logic.compute import SymbolSnapshot, compute_symbol
from kazus_db.models import AlertEvent, AlertState, Coin, Snapshot, SystemStatus

from .alerts import format_alert_batch
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
    entered_symbols_by_tf: dict[str, list[str]] = {tf: [] for tf in alert_timeframes}

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
            entered_timeframes, setup_timeframes = _update_alert_states(db, snap, alert_timeframes)
            db.commit()

        for tf in entered_timeframes:
            entered_symbols_by_tf.setdefault(tf, []).append(symbol)

        # Setup alerts (FVG confirmed in OTE) — sent immediately per symbol.
        for tf in setup_timeframes:
            await _send_setup_alert(settings, snap.symbol, tf)

    for tf in ("D1", "H1"):
        symbols = entered_symbols_by_tf.get(tf, [])
        if not symbols:
            continue
        message = format_alert_batch(symbols, tf)
        with SessionLocal() as db:
            db.add(AlertEvent(timeframe=tf, message=message))
            db.flush()
            _prune_alert_events(db)
            db.commit()
        await send_telegram(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            message,
        )
        with SessionLocal() as db:
            _mark_alerts_sent(db, symbols, tf)
            db.commit()

    _touch_status(last_error)


def _upsert_snapshots(db: Session, snap: SymbolSnapshot) -> None:
    for tf, result, trend, closes in (
        ("D1", snap.global_result, snap.global_trend, snap.global_closes),
        ("H1", snap.local_result, snap.local_trend, snap.local_closes),
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
        row.closes_json = json.dumps(closes) if closes else None
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def _update_alert_states(
    db: Session, snap: SymbolSnapshot, timeframes: Iterable[str]
) -> tuple[list[str], list[str]]:
    """
    Returns (ote_entered_timeframes, setup_entered_timeframes).
    OTE re-entry and setup transition each fire exactly one alert.
    """
    ote_entered: list[str] = []
    setup_entered: list[str] = []

    for tf, result in (("D1", snap.global_result), ("H1", snap.local_result)):
        if tf not in timeframes:
            continue

        row = (
            db.query(AlertState)
            .filter(AlertState.symbol == snap.symbol, AlertState.timeframe == tf)
            .first()
        )
        if row is None:
            row = AlertState(symbol=snap.symbol, timeframe=tf, in_ote=False, in_setup=False)
            db.add(row)
            db.flush()

        prev_in_ote = bool(row.in_ote)
        now_in_ote = bool(result.in_ote)
        prev_in_setup = bool(getattr(row, "in_setup", False))
        now_in_setup = result.setup == "yes"

        if now_in_ote and not prev_in_ote:
            ote_entered.append(tf)

        # Setup fires when: newly in setup (was no, now yes).
        # Reset when price leaves OTE.
        if now_in_setup and not prev_in_setup:
            setup_entered.append(tf)
        if not now_in_ote:
            now_in_setup = False  # reset setup state when OTE exits

        row.in_ote = now_in_ote
        row.in_setup = now_in_setup
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    return ote_entered, setup_entered


async def _send_setup_alert(settings, symbol: str, timeframe: str) -> None:
    fvg_tf = "H1 FVG" if timeframe == "D1" else "M15 FVG"
    message = (
        f"🎯 SETUP: #{symbol} [{timeframe}]\n"
        f"Price is in OTE and closed above nearest {fvg_tf}.\n"
        f"Long entry confirmed."
    )
    logger.info("setup alert: %s %s", symbol, timeframe)
    with SessionLocal() as db:
        db.add(AlertEvent(timeframe=timeframe, message=message))
        db.flush()
        _prune_alert_events(db)
        row = (
            db.query(AlertState)
            .filter(AlertState.symbol == symbol, AlertState.timeframe == timeframe)
            .first()
        )
        if row is not None:
            row.last_setup_alert_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    await send_telegram(settings.telegram_bot_token, settings.telegram_chat_id, message)


def _mark_alerts_sent(db: Session, symbols: list[str], timeframe: str) -> None:
    sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for symbol in symbols:
        row = (
            db.query(AlertState)
            .filter(AlertState.symbol == symbol, AlertState.timeframe == timeframe)
            .first()
        )
        if row is not None:
            row.last_alert_at = sent_at


def _prune_alert_events(db: Session, keep: int = 100) -> None:
    keep_ids = (
        db.query(AlertEvent.id)
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc())
        .limit(keep)
        .subquery()
    )
    db.query(AlertEvent).filter(AlertEvent.id.not_in(keep_ids)).delete(
        synchronize_session=False
    )


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
