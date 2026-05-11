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
from kazus_logic.compute import (
    ALERT_TF_D1,
    ALERT_TF_H1,
    ALERT_TF_H1_M5,
    ALL_ALERT_TFS,
    SymbolSnapshot,
    compute_symbol,
    screener_label_for,
)
from kazus_logic.setup import SetupEvent, SetupState
from kazus_db.models import AlertEvent, AlertState, Coin, Snapshot, SystemStatus

from .chart_renderer import ChartRenderer
from .db import SessionLocal
from .settings import get_settings
from .telegram_alerts import send_setup_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("kazus.worker")


async def run_once(client: BinanceFuturesClient, renderer: ChartRenderer, settings) -> None:
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
            with SessionLocal() as db:
                prev_states = _load_prev_states(db, symbol)

            snap = await compute_symbol(
                client, symbol,
                d1_limit=settings.d1_bar_limit,
                h1_limit=settings.h1_bar_limit,
                prev_states=prev_states,
            )
        except Exception as exc:
            logger.warning("compute failed for %s: %s", symbol, exc)
            last_error = f"{symbol}: {exc}"
            continue

        with SessionLocal() as db:
            _upsert_snapshots(db, snap)
            new_events = _collect_new_events(db, snap, alert_timeframes)
            _persist_setup_states(db, snap)
            db.commit()

        # Fire one Telegram message per new event. Persist the event_id only
        # AFTER successful send (commit-after-send), so a transient Telegram
        # failure causes a retry on the next cycle rather than a lost alert.
        for tf, event in new_events:
            await _dispatch_setup_alert(settings, renderer, snap, tf, event)

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
        row.setup = screener_label_for(snap, tf)
        row.retracement = result.retracement
        row.fib_low = result.fib_low
        row.fib_high = result.fib_high
        row.ote_low_price = result.ote_low_price
        row.ote_high_price = result.ote_high_price
        row.trend = trend
        row.closes_json = json.dumps(closes) if closes else None
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


def _load_prev_states(db: Session, symbol: str) -> dict[str, SetupState]:
    rows = (
        db.query(AlertState)
        .filter(AlertState.symbol == symbol, AlertState.timeframe.in_(ALL_ALERT_TFS))
        .all()
    )
    out: dict[str, SetupState] = {}
    for row in rows:
        s = SetupState.from_json(row.setup_state_json)
        if s is not None:
            out[row.timeframe] = s
    return out


def _persist_setup_states(db: Session, snap: SymbolSnapshot) -> None:
    for tf, state in snap.setup_states.items():
        row = (
            db.query(AlertState)
            .filter(AlertState.symbol == snap.symbol, AlertState.timeframe == tf)
            .first()
        )
        if row is None:
            row = AlertState(symbol=snap.symbol, timeframe=tf, in_ote=False)
            db.add(row)
            db.flush()
        row.setup_state_json = state.to_json()
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

    # The HTF zone result for each alert tf — D1 zone for "D1", H1 zone
    # for "H1" and "H1-M5".
    zone_for_tf = {
        ALERT_TF_D1: snap.global_result,
        ALERT_TF_H1: snap.local_result,
        ALERT_TF_H1_M5: snap.local_result,
    }

    for tf in ALL_ALERT_TFS:
        if tf not in timeframes:
            continue
        if tf not in snap.setup_events:
            # H1-M5 only exists for the M5_SYMBOLS subset.
            continue

        result = zone_for_tf[tf]
        events = snap.setup_events[tf]

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


async def _dispatch_setup_alert(
    settings,
    renderer: ChartRenderer,
    snap: SymbolSnapshot,
    timeframe: str,
    event: SetupEvent,
) -> None:
    """Render + send a setup alert, and persist the dedup record on success.

    Charting + Telegram delivery live in telegram_alerts.send_setup_alert;
    this wrapper handles the DB-side bookkeeping (sent_event_ids, AlertEvent
    feed, last_setup_alert_at).
    """
    logger.info(
        "setup alert: %s %s kind=%s id=%s",
        snap.symbol, timeframe, event.kind, event.event_id,
    )
    sent, caption = await send_setup_alert(settings, renderer, snap, timeframe, event)
    if not sent:
        # Don't persist the event_id — let the next cycle retry.
        return

    with SessionLocal() as db:
        row = (
            db.query(AlertState)
            .filter(AlertState.symbol == snap.symbol, AlertState.timeframe == timeframe)
            .first()
        )
        if row is None:
            row = AlertState(symbol=snap.symbol, timeframe=timeframe, in_ote=True)
            db.add(row)
            db.flush()
        sent_ids = _load_sent_ids(row.sent_event_ids)
        sent_ids.add(event.event_id)
        row.sent_event_ids = json.dumps(sorted(sent_ids))
        row.last_setup_alert_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(AlertEvent(timeframe=timeframe, message=caption))
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
    renderer = ChartRenderer(settings)
    try:
        while not stop_event.is_set():
            try:
                await run_once(client, renderer, settings)
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=settings.refresh_interval_sec
                )
            except asyncio.TimeoutError:
                continue
    finally:
        await renderer.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
