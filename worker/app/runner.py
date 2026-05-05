"""
Worker entry-point.

Responsibilities:
- Periodically (every REFRESH_INTERVAL_SEC) pull the list of active coins
  from Postgres.
- For each coin fetch D1 and H1 klines from Binance Futures and compute
  the KazusGlobal (D1) + KazusLocal (H1) snapshot.
- Upsert the result into `snapshots`.
- Drive a per-(symbol, timeframe) event-stream alert state: while price is
  in OTE, fire one Telegram message per new SetupEvent (STB / Inversion /
  Created), deduped by event_id. Reset the sent set when price leaves OTE.
- Update `system_status.last_refresh_at` and `last_error`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone
from typing import Iterable, List, Tuple

from sqlalchemy.orm import Session

from kazus_logic.binance import BinanceFuturesClient
from kazus_logic.compute import SetupEvent, SymbolSnapshot, compute_symbol
from kazus_logic.engine import Bar, ZoneResult
from kazus_db.models import AlertEvent, AlertState, Coin, Snapshot, SystemStatus

from .chart_image import render_htf_png, render_setup_png
from .db import SessionLocal
from .settings import get_settings
from .telegram import send_telegram, send_telegram_media_group, send_telegram_photo

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
            new_events = _collect_new_events(db, snap, alert_timeframes)
            db.commit()

        # Fire one Telegram message per new event. Persist the event_id only
        # AFTER successful send (commit-after-send), so a transient Telegram
        # failure causes a retry on the next cycle rather than a lost alert.
        for tf, event in new_events:
            await _send_setup_alert(settings, snap, tf, event)

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
        row.setup = result.setup or ""
        row.retracement = result.retracement
        row.fib_low = result.fib_low
        row.fib_high = result.fib_high
        row.ote_low_price = result.ote_low_price
        row.ote_high_price = result.ote_high_price
        row.trend = trend
        row.closes_json = json.dumps(closes) if closes else None
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def _collect_new_events(
    db: Session, snap: SymbolSnapshot, timeframes: Iterable[str]
) -> List[Tuple[str, SetupEvent]]:
    """
    For each enabled timeframe, dedupe the snapshot's setup events against
    AlertState.sent_event_ids. Returns events that should fire NOW; commit
    of `sent_event_ids` happens after each successful Telegram send so that
    failed sends retry on the next cycle.

    On OTE exit, the per-(symbol, timeframe) sent set is cleared so a later
    re-entry restarts the event stream.
    """
    pending: List[Tuple[str, SetupEvent]] = []

    for tf, result, events in (
        ("D1", snap.global_result, snap.global_setup_events),
        ("H1", snap.local_result, snap.local_setup_events),
    ):
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

        now_in_ote = bool(result.in_ote)

        if not now_in_ote:
            row.in_ote = False
            row.sent_event_ids = "[]"
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            continue

        sent_ids = _load_sent_ids(row.sent_event_ids)
        new_for_tf: List[SetupEvent] = []
        for event in events:
            if event.event_id in sent_ids:
                continue
            new_for_tf.append(event)

        row.in_ote = True
        # Persist OTE entry/state immediately; sent_event_ids is updated
        # per-event in _send_setup_alert after Telegram acknowledges.
        if row.sent_event_ids is None:
            row.sent_event_ids = "[]"
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

        for event in new_for_tf:
            pending.append((tf, event))

    return pending


def _load_sent_ids(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data}


async def _send_setup_alert(
    settings, snap: SymbolSnapshot, timeframe: str, event: SetupEvent
) -> None:
    if timeframe == "D1":
        result: ZoneResult = snap.global_result
        bars: list[Bar] = list(snap.global_confirmation_bars)
        htf_bars: list[Bar] = list(snap.global_htf_bars)
        confirmation_tf = "H1"
    else:
        result = snap.local_result
        bars = list(snap.local_confirmation_bars)
        htf_bars = list(snap.local_htf_bars)
        confirmation_tf = "M15"

    symbol = snap.symbol
    price_str = f"{snap.price:g}" if snap.price else "—"
    retr_str = (
        f"{result.retracement * 100:.1f}%" if result.retracement is not None else "—"
    )
    ote_str = (
        f"{result.ote_low_price:g} – {result.ote_high_price:g}"
        if result.ote_low_price is not None and result.ote_high_price is not None
        else "—"
    )
    fvg_str = f"{event.fvg_bottom:g} – {event.fvg_top:g}"
    message = (
        f"🎯 <b>{event.kind}</b> setup: #{symbol} [{timeframe}]\n"
        f"{event.direction} trend, retracement {retr_str}\n"
        f"Price: {price_str}\n"
        f"OTE:   {ote_str}\n"
        f"FVG:   {fvg_str}\n"
        f"Confirmation: {confirmation_tf} bars"
    )
    logger.info(
        "setup alert: %s %s kind=%s id=%s",
        symbol, timeframe, event.kind, event.event_id,
    )

    htf_photo = render_htf_png(
        symbol, timeframe, htf_bars, result, event.kind, event.fvg_top, event.fvg_bottom
    )
    confirm_photo = render_setup_png(
        symbol, timeframe, confirmation_tf, bars, result,
        event.kind, event.fvg_top, event.fvg_bottom,
    )

    photos = [p for p in (htf_photo, confirm_photo) if p is not None]
    sent = False
    if len(photos) >= 2:
        sent = await send_telegram_media_group(
            settings.telegram_bot_token, settings.telegram_chat_id, photos, message
        )
    elif len(photos) == 1:
        sent = await send_telegram_photo(
            settings.telegram_bot_token, settings.telegram_chat_id, photos[0], message
        )
    if not sent:
        sent = await send_telegram(
            settings.telegram_bot_token, settings.telegram_chat_id, message
        )

    if not sent:
        # Don't persist the event_id — let the next cycle retry.
        return

    # Telegram acknowledged: persist the event_id (plus any STB-suppressed
    # ids) so subsequent cycles dedupe.
    with SessionLocal() as db:
        row = (
            db.query(AlertState)
            .filter(AlertState.symbol == symbol, AlertState.timeframe == timeframe)
            .first()
        )
        if row is None:
            row = AlertState(symbol=symbol, timeframe=timeframe, in_ote=True)
            db.add(row)
            db.flush()
        sent_ids = _load_sent_ids(row.sent_event_ids)
        sent_ids.add(event.event_id)
        sent_ids.update(event.suppresses)
        row.sent_event_ids = json.dumps(sorted(sent_ids))
        row.last_setup_alert_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(AlertEvent(timeframe=timeframe, message=message))
        db.flush()
        _prune_alert_events(db)
        db.commit()


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
