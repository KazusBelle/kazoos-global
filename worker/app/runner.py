"""
Worker entry-point.

Responsibilities:
- Wake on every M5 candle boundary (+close_check_delay_sec) and process the
  alert timeframes whose candle just closed: H1-M5 every 5m, H1 every 15m,
  D1 (H1-confirmation) every hour. A full re-check of all timeframes runs
  on startup and after any missed tick as a safety net.
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
import faulthandler
import json
import logging
import signal
import sys
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Set, Tuple

from sqlalchemy.orm import Session

from kazus_logic.binance import BinanceFuturesClient
from kazus_logic.compute import (
    ALERT_TF_D1,
    ALERT_TF_H1,
    ALERT_TF_H1_M5,
    ALL_ALERT_TFS,
    M5_SYMBOLS,
    SymbolSnapshot,
    compute_symbol,
    screener_label_for,
)
from kazus_logic.setup import SetupEvent, SetupState
from kazus_db.models import AlertEvent, AlertState, Coin, Snapshot, SystemStatus

from .chart_renderer import ChartRenderer
from .db import SessionLocal
from .settings import get_settings
from .telegram import send_telegram
from .telegram_alerts import send_setup_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("kazus.worker")

# ── TEMPORARY event-loop stall detector (Day-3 diagnostic) ──────────────────
# An asyncio task bumps _LOOP_HB every second; a plain daemon thread (NOT on
# the event loop, so it keeps running even when the loop is wedged) watches it
# and, when the heartbeat goes stale, dumps every thread's stack to stderr.
# This pinpoints the synchronous blocking frame that freezes the loop.
_LOOP_HB = _time.monotonic()
_STALL_DUMP_THRESHOLD_S = 20.0


async def _loop_heartbeat(stop_event: asyncio.Event) -> None:
    global _LOOP_HB
    while not stop_event.is_set():
        _LOOP_HB = _time.monotonic()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            break
        except asyncio.TimeoutError:
            continue


def _stall_monitor() -> None:
    last_dump_for = 0.0
    while True:
        _time.sleep(5.0)
        lag = _time.monotonic() - _LOOP_HB
        if lag > _STALL_DUMP_THRESHOLD_S and lag != last_dump_for:
            last_dump_for = lag
            sys.stderr.write(
                f"\n=== EVENT-LOOP STALL lag={lag:.1f}s @ {datetime.now(timezone.utc).isoformat()} — ALL THREAD STACKS ===\n"
            )
            faulthandler.dump_traceback(all_threads=True)
            sys.stderr.flush()


async def run_once(
    client: BinanceFuturesClient,
    renderer: ChartRenderer,
    settings,
    due_tfs: Set[str],
) -> None:
    """Process one wake-up.

    ``due_tfs`` is the set of alert timeframes whose candle just closed —
    only those are dispatched. When the only due timeframe is H1-M5 the
    coin list is narrowed to M5_SYMBOLS, since H1-M5 setups exist only for
    BTC/ETH/SOL and there is nothing else to check on a bare 5m boundary.
    """
    with SessionLocal() as db:
        coins: list[str] = [
            c.symbol
            for c in db.query(Coin).filter(Coin.is_active.is_(True)).order_by(Coin.symbol.asc()).all()
        ]

    # A bare M5 boundary only concerns the H1-M5 timeframe, which is
    # BTC/ETH/SOL-only — skip every other coin entirely.
    if due_tfs == {ALERT_TF_H1_M5}:
        coins = [c for c in coins if c in M5_SYMBOLS]

    if not coins:
        logger.info("no coins due this tick — skipping")
        _touch_status(None)
        return

    # Dispatch is gated to the timeframes that are both enabled and due.
    alert_timeframes = {
        t.strip() for t in settings.alert_timeframes.split(",") if t.strip()
    } & due_tfs

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

        # OTE exit no longer wipes sent_event_ids. A brief tick out of
        # the band followed by a return must not let already-sent INV/STB
        # re-alert on the same visual base; the detector now carries the
        # SetupState across that gap, and the dedup set must match.
        # AlertState row is updated either way so the in_ote flag stays
        # in sync — the events list is just empty when no triggers fired.
        sent_ids = _load_sent_ids(row.sent_event_ids)
        new_for_tf: List[SetupEvent] = []
        for event in events:
            if event.event_id in sent_ids:
                continue
            new_for_tf.append(event)

        row.in_ote = now_in_ote
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


def _next_m5_boundary(now: datetime) -> datetime:
    """First M5 candle boundary strictly after ``now`` (UTC, seconds zeroed).

    Anchored to the top of the hour so it stays correct across the hour
    rollover (top-of-hour + N*5min)."""
    base = now.replace(minute=0, second=0, microsecond=0)
    elapsed = now - base
    slot = elapsed // timedelta(minutes=5) + 1
    return base + slot * timedelta(minutes=5)


def _due_timeframes(boundary: datetime) -> Set[str]:
    """Alert timeframes whose candle closes at this M5 boundary.

    H1-M5 closes on every 5m boundary, H1 every 15m, D1 (H1-confirmation)
    on the hour. Simultaneous closes at :00 simply yield all three — they
    are processed sequentially in one pass, never concurrently."""
    due: Set[str] = {ALERT_TF_H1_M5}
    if boundary.minute % 15 == 0:
        due.add(ALERT_TF_H1)
    if boundary.minute == 0:
        due.add(ALERT_TF_D1)
    return due


_ANOMALY_INTERVAL_S = 300
_INTEL_SNAPSHOT_INTERVAL_S = 300
_INVESTIGATION_AUTODRAFT_INTERVAL_S = 300
# Integrity Repair Pass: PENDING-capture drain runs faster than auto-draft
# so freshly-created cases get a frozen snapshot within ~30s rather than
# waiting up to 5 minutes for the autodraft tick.
_INVESTIGATION_CAPTURE_INTERVAL_S = 30


async def _anomaly_loop(stop_event: asyncio.Event) -> None:
    """Periodically scan recent intelligence layers and auto-record
    anomalies + genealogy edges. Phase 13 added the auto-linker so
    each new anomaly is wired into the memory graph the moment it
    lands."""
    from kazus_logic.liquidity.research import auto_record_anomalies_with_links

    # Initial delay so the worker isn't fighting the first poll cycle.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=30.0)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            # Heavy synchronous DB scan (structural_breaks → _interaction_in_window
            # issues a multi-minute query) — run it in a thread so it cannot block
            # the event loop / starve realtime ingestion. Session is opened inside
            # the thread (not shared across threads).
            def _run():
                with SessionLocal() as db:
                    return auto_record_anomalies_with_links(db)
            result = await asyncio.to_thread(_run)
            inserted = len(result.get("inserted") or [])
            edges = len(result.get("edges") or [])
            if inserted or edges:
                logger.info("auto-anomaly scan: recorded %d · linked %d edges", inserted, edges)
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto-anomaly scan failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_ANOMALY_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            continue


async def _intel_snapshot_loop(stop_event: asyncio.Event) -> None:
    """Persist a snapshot of the engine's aggregate state so the
    evolution timeline + market cycle decomposition have history to
    plot. Mirrors the anomaly cadence (5 min)."""
    from kazus_logic.liquidity.research import snapshot_intelligence_history

    # Offset start so the snapshot doesn't fire at the same instant as
    # the anomaly scan (DB-load smoothing).
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=90.0)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            def _run():
                with SessionLocal() as db:
                    return snapshot_intelligence_history(db)
            row = await asyncio.to_thread(_run)
            logger.debug("intel snapshot persisted id=%s state=%s", row["id"], row["coordinated_state"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("intel snapshot failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_INTEL_SNAPSHOT_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            continue


async def _investigation_capture_loop(stop_event: asyncio.Event) -> None:
    """Integrity Repair Pass: drain the PENDING capture queue on a
    fast cadence (30s). Decouples case creation from the 8-layer
    intelligence-surface cascade — case creation is now a single
    insert, and frozen snapshot lands a few seconds later in the
    background. Failures are recorded on the case (FAILED status +
    capture_error); operator can retry via the API."""
    from kazus_logic.liquidity.research import investigation_capture_pending

    # Brief delay so the first tick doesn't fight the initial cycle.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=15.0)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            def _run():
                with SessionLocal() as db:
                    return investigation_capture_pending(db, limit=20)
            result = await asyncio.to_thread(_run)
            if result.get("captured_ids") or result.get("failed_ids"):
                logger.info(
                    "investigation capture drained: %d captured · %d failed",
                    len(result.get("captured_ids") or []),
                    len(result.get("failed_ids") or []),
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("investigation capture drain failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_INVESTIGATION_CAPTURE_INTERVAL_S)
            break
        except asyncio.TimeoutError:
            continue


async def _investigation_autodraft_loop(stop_event: asyncio.Event) -> None:
    """Phase-18 auto-draft loop. Polls crisis_genesis and opens a draft
    investigation case whenever the verdict transitions to PRE_CASCADE
    on a new probe-composition fingerprint. Idempotent — dedups against
    any active case with the same fingerprint."""
    from kazus_logic.liquidity.research import investigation_auto_draft_tick

    # Stagger initial start so this doesn't fight the first poll cycle
    # or the anomaly/intel snapshots.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=120.0)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            def _run():
                with SessionLocal() as db:
                    return investigation_auto_draft_tick(db)
            drafted = await asyncio.to_thread(_run)
            if drafted:
                logger.info(
                    "investigation auto-drafted: id=%s title=%s",
                    drafted.get("id"), drafted.get("title"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("investigation autodraft failed: %s", exc)
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=_INVESTIGATION_AUTODRAFT_INTERVAL_S,
            )
            break
        except asyncio.TimeoutError:
            continue


# ── Collection watchdog (Day-1) ─────────────────────────────────────────────
# Value-based continuity guard for realtime collection. It does NOT trust
# row-recency or frames_total alone — a stall is depth VALUE going stale/frozen
# even while bookTicker frames keep advancing (the masking failure seen during
# R2). Anchored on a DECLARED baseline (env, not live pins) so a pin-collapse is
# caught rather than silenced. Alerts via the existing send_telegram path on
# GREEN→RED transition + recovery on RED→GREEN. All credible_depth/burst checks
# are measured over the stall-threshold window, so a short self-recovering WS
# quiet-spell (minutes) does not trip a 15-min threshold.

def _watchdog_baseline() -> list[str]:
    raw = get_settings().watchdog_baseline
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def evaluate_collection_state(
    db: Session, *, baseline: list[str], stall_threshold_s: int, conn_storm: int
) -> Tuple[str, List[str], dict]:
    """Return (state, reasons, metrics). state in {'GREEN','RED'}."""
    from sqlalchemy import text as _t

    h = db.execute(_t(
        "SELECT subscribed_count, frames_total, conn_id "
        "FROM liquidity_runtime_health ORDER BY created_at DESC LIMIT 1")).first()
    subscribed = int(h[0]) if h and h[0] is not None else 0
    frames = int(h[1]) if h and h[1] is not None else 0
    conn = int(h[2]) if h and h[2] is not None else 0

    rows = db.execute(_t(
        "SELECT symbol, "
        " round((extract(epoch from now())*1000 - max(ts))/1000.0,1) AS age_s, "
        " count(DISTINCT value) AS dv, count(value) AS nonnull "
        "FROM liquidity_samples "
        "WHERE metric='credible_depth' AND symbol = ANY(:syms) "
        "  AND ts > (extract(epoch from now()) - :win)*1000 "
        "GROUP BY symbol"), {"syms": list(baseline), "win": stall_threshold_s}).all()
    by_sym = {r[0]: (float(r[1]), int(r[2]), int(r[3])) for r in rows}

    bursts = db.execute(_t(
        "SELECT count(*) FROM liquidity_bursts "
        "WHERE created_at > now() - (:w * interval '1 second')"),
        {"w": stall_threshold_s}).scalar() or 0
    churn = db.execute(_t(
        "SELECT coalesce(max(conn_id)-min(conn_id),0) FROM liquidity_runtime_health "
        "WHERE created_at > now() - (:w * interval '1 second')"),
        {"w": stall_threshold_s}).scalar() or 0

    reasons: List[str] = []
    if subscribed < len(baseline):
        reasons.append(f"subscribed_count={subscribed}<{len(baseline)}")
    for sym in baseline:
        v = by_sym.get(sym)
        if v is None:
            reasons.append(f"{sym}: no credible_depth in {stall_threshold_s}s")
            continue
        age_s, dv, nonnull = v
        if age_s > stall_threshold_s:
            reasons.append(f"{sym}: credible_depth stale {age_s}s")
        elif dv < 2 or nonnull == 0:
            reasons.append(f"{sym}: credible_depth value frozen (dv={dv})")
    if int(bursts) == 0:
        reasons.append(f"no bursts in {stall_threshold_s}s")
    if int(churn) > conn_storm:
        reasons.append(f"conn_id churn {churn}>{conn_storm}")

    value_stalled = any(("stale" in r) or ("frozen" in r) or ("no credible_depth" in r) for r in reasons)
    if value_stalled and frames > 0:
        reasons.append("frames advancing while value-path stalled (masking)")

    state = "RED" if reasons else "GREEN"
    metrics = {"subscribed": subscribed, "frames": frames, "conn": conn,
               "bursts": int(bursts), "conn_churn": int(churn)}
    return state, reasons, metrics


async def _watchdog_send(text_msg: str) -> bool:
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        logger.warning("watchdog: telegram not configured; skipping: %s", text_msg)
        return False
    try:
        return await send_telegram(s.telegram_bot_token, s.telegram_chat_id, text_msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("watchdog telegram send failed: %s", exc)
        return False


async def _collection_watchdog_loop(stop_event: asyncio.Event) -> None:
    s = get_settings()
    baseline = _watchdog_baseline()
    poll = max(10, int(s.watchdog_poll_seconds))
    threshold = max(30, int(s.watchdog_stall_threshold_seconds))
    conn_storm = int(s.watchdog_conn_storm)
    logger.info(
        "collection watchdog started (baseline=%s poll=%ss threshold=%ss)",
        baseline, poll, threshold,
    )
    alerted = False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=min(float(poll), 60.0))
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        try:
            with SessionLocal() as db:
                state, reasons, metrics = evaluate_collection_state(
                    db, baseline=baseline, stall_threshold_s=threshold, conn_storm=conn_storm
                )
            if state == "RED" and not alerted:
                alerted = True
                msg = (
                    "\U0001F534 LIQ COLLECTION STALL\n"
                    + "; ".join(reasons[:6])
                    + f"\nsubscribed={metrics['subscribed']} bursts={metrics['bursts']} "
                    f"frames_total={metrics['frames']} conn_churn={metrics['conn_churn']}"
                )
                await _watchdog_send(msg)
                logger.warning("collection watchdog RED: %s", reasons)
            elif state == "GREEN" and alerted:
                alerted = False
                await _watchdog_send("\U0001F7E2 LIQ COLLECTION RESUMED — value-path healthy again.")
                logger.info("collection watchdog recovered")
        except Exception as exc:  # noqa: BLE001
            logger.exception("collection watchdog failed: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(poll))
            break
        except asyncio.TimeoutError:
            continue


async def main() -> None:
    settings = get_settings()
    logger.info(
        "worker starting; M5-boundary scheduler, close delay %ss",
        settings.close_check_delay_sec,
    )

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

    # Liquidity polling runs as an independent background task on its own
    # 60s cadence — it doesn't share the M5-boundary scheduler because
    # liquidity sampling needs to be smooth, not tied to candle closes.
    from kazus_logic.liquidity.poller import loop as _liquidity_loop
    from kazus_logic.liquidity.realtime.engine import RealtimeEngine
    liquidity_task = asyncio.create_task(
        _liquidity_loop(SessionLocal, stop_event), name="liquidity-poller"
    )
    realtime_engine = RealtimeEngine(SessionLocal)
    realtime_task = asyncio.create_task(
        realtime_engine.run(stop_event), name="liquidity-realtime"
    )
    # Heavy worker-side analytics loops (anomaly recorder, intel snapshot,
    # investigation autodraft/capture). These call into kazus_logic.liquidity.
    # research and run AVG(value)/percentile_disc aggregations over the 66M-row
    # liquidity_samples — the worker-side source of the host-CPU saturation.
    # Gated behind WORKER_RESEARCH_ANALYTICS_ENABLED so they can be shed in an
    # emergency WITHOUT touching core collection above.
    if settings.worker_research_analytics_enabled:
        # Phase-12 auto-anomaly recorder — slow loop (5 min).
        anomaly_task = asyncio.create_task(
            _anomaly_loop(stop_event), name="liquidity-anomaly-recorder"
        )
        intel_snapshot_task = asyncio.create_task(
            _intel_snapshot_loop(stop_event), name="liquidity-intel-snapshot"
        )
        # Phase-18 investigation auto-draft loop — opens draft cases on
        # PRE_CASCADE genesis verdicts so post-mortem has a starting point.
        investigation_task = asyncio.create_task(
            _investigation_autodraft_loop(stop_event), name="investigation-autodraft"
        )
        # Integrity Repair Pass: fast-cadence PENDING-capture drain.
        investigation_capture_task = asyncio.create_task(
            _investigation_capture_loop(stop_event), name="investigation-capture"
        )
    else:
        anomaly_task = intel_snapshot_task = investigation_task = investigation_capture_task = None
        logger.warning(
            "WORKER_RESEARCH_ANALYTICS_ENABLED=false — heavy research/intelligence "
            "loops DISABLED (anomaly, intel-snapshot, investigation autodraft/capture). "
            "Core collection (poller, realtime, watchdog, heartbeat) unaffected."
        )
    # Day-1 value-based collection watchdog — protects continuity across the
    # long wait; isolated sibling task, exceptions caught, never blocks ingest.
    collection_watchdog_task = asyncio.create_task(
        _collection_watchdog_loop(stop_event), name="collection-watchdog"
    )
    # TEMPORARY (Day-3): loop-stall stack dumper.
    loop_hb_task = asyncio.create_task(_loop_heartbeat(stop_event), name="loop-heartbeat")
    threading.Thread(target=_stall_monitor, name="stall-monitor", daemon=True).start()
    # A tick gap wider than this means a boundary was missed (slow cycle,
    # restart, API outage) — the next tick then re-checks every timeframe.
    gap_threshold = timedelta(minutes=7)
    last_tick: datetime | None = None
    first_run = True
    try:
        while not stop_event.is_set():
            now = datetime.now(timezone.utc)
            boundary = _next_m5_boundary(now)
            target = boundary + timedelta(seconds=settings.close_check_delay_sec)
            wait_s = (target - now).total_seconds()
            if wait_s > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
                    break  # stop signalled while waiting
                except asyncio.TimeoutError:
                    pass

            tick = datetime.now(timezone.utc)
            due_tfs = _due_timeframes(boundary)
            # Safety net: on startup or after a missed boundary, fall back
            # to a full re-check of every timeframe so no setup is lost.
            if first_run or (last_tick is not None and tick - last_tick > gap_threshold):
                logger.info("full re-check (startup or missed tick)")
                due_tfs = set(ALL_ALERT_TFS)

            try:
                _scan_t0 = datetime.now(timezone.utc)
                logger.info("M5 scan start (tfs=%s)", sorted(due_tfs))
                await run_once(client, renderer, settings, due_tfs)
                logger.info(
                    "M5 scan end (%.1fs)",
                    (datetime.now(timezone.utc) - _scan_t0).total_seconds(),
                )
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)

            last_tick = tick
            first_run = False
    finally:
        for t in (liquidity_task, realtime_task, anomaly_task, intel_snapshot_task, investigation_task, investigation_capture_task, collection_watchdog_task, loop_hb_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await renderer.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
