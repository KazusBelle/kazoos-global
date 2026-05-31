"""Top-level realtime engine.

Owns one FuturesWsClient. Periodically reads the desired-subscription
set from `liquidity_active_subs` (managed via the heartbeat HTTP
endpoint), reconciles WS subscriptions, dispatches incoming frames to
per-symbol SymbolState, and samples metrics into liquidity_samples at
1Hz with batched writes.

Streams subscribed per active symbol:
  <s>@depth20@100ms   — orderbook top-20 every 100ms (Credible Depth, OBI)
  <s>@bookTicker      — best bid/ask, sub-100ms (mid_price)
  <s>@trade           — trades tape (Kyle Lambda / Impact / Fragility)

Note on missing streams (operator-reality 2026-05-25):
  Binance Futures `<s>@aggTrade` and `<s>@forceOrder` are silently
  unavailable from this network perimeter — SUBSCRIBE returns success
  but zero frames ever arrive (verified at the wire level; SPOT
  aggTrade and FUTURES @trade work, only aggTrade/forceOrder on
  futures do not). We therefore use the non-aggregated `<s>@trade`
  stream which carries the same fields we read (p, q, m, T) and is
  delivered normally. Liquidation Stress (`liq_stress`) is dropped
  from the metric registry rather than written as silently-zero
  rows — operator surfaces never paint a fabricated value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Set

import websockets

from .exec_impact import (
    detect_and_measure_bursts,
    detect_exec_validation_records,
    rolling_exec_metrics,
)
from .intelligence import (
    fragility_score,
    impact_score,
    kyle_lambda,
    recovery_time_ms,
    refill_velocity_usd_per_s,
    resiliency_score,
    update_intelligence,
)
from . import health
from .burst import detect_bursts
from .resiliency import detect_resiliency
from .metrics import credible_depth_sides, liquidation_stress_usd, obi_rt, persistence_quality
from .orderbook import Liquidation, SymbolState, Trade
from .ws_client import FuturesWsClient

logger = logging.getLogger("kazus.liquidity.realtime")

_STREAM_SUFFIXES = ("depth20@100ms", "bookTicker", "trade")

# Dedicated all-market liquidation feed (LIQ STRESS restoration). The main
# per-symbol socket is on `/stream` (default class), which does NOT deliver
# forceOrder; the `/market` class does (isolation-matrix verified). This is a
# separate always-on raw single-stream connection — no SUBSCRIBE — that the
# main FuturesWsClient is untouched by.
FORCE_ORDER_WS_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"

RECONCILE_INTERVAL_S = 5
SAMPLE_INTERVAL_S = 1.0
FLUSH_INTERVAL_S = 5.0
STATUS_INTERVAL_S = 3.0
# Hard cap on how many pinned symbols we'll stream simultaneously — protects
# against accidental pin-the-whole-top-100. The active (currently-opened)
# symbol is always added on top of this cap so opening a chart never gets
# blocked by a full pin list.
PIN_CAP = 20


def _streams_for(symbol: str) -> List[str]:
    s = symbol.lower()
    return [f"{s}@{suf}" for suf in _STREAM_SUFFIXES]


class RealtimeEngine:
    def __init__(self, db_factory) -> None:
        self.db_factory = db_factory
        self.ws = FuturesWsClient()
        self.states: Dict[str, SymbolState] = {}
        self.desired: Set[str] = set()
        self.subscribed: Set[str] = set()
        self._known_conn_id: int = 0
        self._sample_buffer: list[dict] = []
        self._burst_buffer: list[dict] = []
        self._exec_val_buffer: list[dict] = []
        self._resiliency_buffer: list[dict] = []  # PHASE 4A (additive over 3A/3B)
        self._last_message_at: float = 0.0  # epoch seconds of latest frame
        # ── Runtime-health stage probes (WS_RELIABILITY_001) — additive,
        # in-memory only; read by the heartbeat, never feed any metric. ──
        self._frames_total: int = 0
        self._last_sample_ms: int = 0
        self._samples_total: int = 0
        self._flush_started_ms: int = 0
        self._flush_completed_ms: int = 0
        self._flush_duration_ms: float = 0.0
        self._flush_rows_total: int = 0

    # ── desired-set reconciliation ────────────────────────────────────────

    async def _read_desired(self) -> Set[str]:
        """Return the union of:

        * active subs (liquidity_active_subs) whose expires_at is still in
          the future — short-lived heartbeats from an opened chart;
        * pinned symbols (liquidity_pins) — persistent, capped at PIN_CAP
          ordered by `pinned_order` ascending (the visually-top pins win).

        Active subs are never capped (it's the symbol the user is looking
        at right now), and they're added AFTER the pin cap so they always
        get a slot even if PIN_CAP pins are already in the desired set.
        """
        from kazus_db.models import LiquidityActiveSub, LiquidityPin
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.db_factory() as db:
            pins = (
                db.query(LiquidityPin)
                .order_by(LiquidityPin.pinned_order.asc())
                .limit(PIN_CAP)
                .all()
            )
            actives = (
                db.query(LiquidityActiveSub)
                .filter(LiquidityActiveSub.expires_at > now_naive)
                .all()
            )
        out: Set[str] = {p.symbol.upper() for p in pins}
        out.update(a.symbol.upper() for a in actives)
        return out

    async def _reconcile(self) -> None:
        try:
            desired = await self._read_desired()
        except Exception as exc:  # noqa: BLE001
            logger.exception("read desired subs failed: %s", exc)
            return

        # If the WS reconnected since last check, re-issue every desired
        # subscription. Binance forgets our subs across reconnect.
        if self.ws.conn_id != self._known_conn_id:
            logger.info(
                "ws reconnect detected (conn_id %d → %d); re-subscribing all",
                self._known_conn_id, self.ws.conn_id,
            )
            self.subscribed.clear()
            self._known_conn_id = self.ws.conn_id
            # Mark a tape discontinuity on every live symbol: prints may have
            # been missed across the reconnect, so Burst Detection refuses
            # (DROPPED) any burst spanning the gap rather than fabricating one.
            # Also reset the warmup anchor — post-reconnect tape is partial.
            gap_ms = int(time.time() * 1000)
            for st in self.states.values():
                st.tape_gap_ts = gap_ms
                st.tape_started_ts = None

        to_add = desired - self.subscribed
        to_drop = self.subscribed - desired

        if to_add:
            streams: List[str] = []
            for sym in to_add:
                streams.extend(_streams_for(sym))
                self.states.setdefault(sym, SymbolState(symbol=sym))
            try:
                await self.ws.subscribe(streams)
                self.subscribed |= to_add
            except Exception as exc:  # noqa: BLE001
                logger.warning("subscribe failed for %s: %s", to_add, exc)

        if to_drop:
            streams = []
            for sym in to_drop:
                streams.extend(_streams_for(sym))
            try:
                await self.ws.unsubscribe(streams)
            except Exception as exc:  # noqa: BLE001
                logger.warning("unsubscribe failed for %s: %s", to_drop, exc)
            self.subscribed -= to_drop
            for sym in to_drop:
                self.states.pop(sym, None)

        self.desired = desired

    # ── frame dispatch ────────────────────────────────────────────────────

    def _on_frame(self, stream: str, data: dict) -> None:
        # stream format: "<symbol>@<suffix>"
        try:
            sym_lower, _, suffix = stream.partition("@")
        except Exception:  # noqa: BLE001
            return
        if not sym_lower or not suffix:
            return
        symbol = sym_lower.upper()
        state = self.states.get(symbol)
        if state is None:
            return  # late frame for an unsubscribed symbol

        now_ms = int(time.time() * 1000)
        self._last_message_at = now_ms / 1000.0
        self._frames_total += 1  # health probe: reader is draining the socket
        try:
            if suffix.startswith("depth20"):
                # data fields: "b": [["price","qty"], ...], "a": [...]
                bids = [(float(p), float(q)) for p, q in data.get("b", [])]
                asks = [(float(p), float(q)) for p, q in data.get("a", [])]
                state.apply_depth20(bids, asks, now_ms)
            elif suffix == "bookTicker":
                # fields: "b" best bid px, "a" best ask px (per @bookTicker)
                best_bid = float(data.get("b") or 0.0)
                best_ask = float(data.get("a") or 0.0)
                if best_bid > 0 and best_ask > 0:
                    ts = int(data.get("E") or data.get("T") or now_ms)
                    state.apply_book_ticker(best_bid, best_ask, ts)
            elif suffix == "trade":
                # `<s>@trade` (non-aggregated). Same field shape as
                # aggTrade for the parts we read: T/E timestamps, p/q
                # price/qty strings, m=is-buyer-maker. We don't read the
                # aggregation-specific fields (a/f/l) so the swap from
                # aggTrade is transparent to downstream metrics.
                ts = int(data.get("E") or data.get("T") or now_ms)
                price = float(data.get("p") or 0.0)
                qty = float(data.get("q") or 0.0)
                is_buyer_maker = bool(data.get("m", False))
                if price > 0 and qty > 0:
                    state.push_trade(Trade(ts=ts, price=price, qty=qty, is_buyer_maker=is_buyer_maker))
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("frame parse failed for %s: %s", stream, exc)

    # ── sampling + flush ──────────────────────────────────────────────────

    def _sample_all(self) -> None:
        """One round of metric snapshots for every subscribed symbol.
        Results go into self._sample_buffer; the flusher drains it."""
        now_ms = int(time.time() * 1000)
        for symbol, state in self.states.items():
            mid = state.mid_price()
            # Single survivorship pass → combined + per-side + observable
            # imbalance, all from one (price, age) walk so they cannot drift
            # apart and all share this tick's now_ms (replay-deterministic).
            # bid_d/ask_d are both None iff mid is unavailable → UNKNOWN
            # propagates uniformly into every derived output below.
            bid_d, ask_d = credible_depth_sides(state, now_ms)
            depth = None if bid_d is None else bid_d + ask_d
            credible_delta = None if bid_d is None else bid_d - ask_d
            # Intelligence layer needs the latest depth before we sample
            # the resiliency/kyle outputs — it advances the recovery
            # state machine and records the depth sample for the rolling
            # history buffers.
            update_intelligence(state, now_ms, depth)
            # Exec-impact layer: forward-only realized-vs-predicted
            # measurement. Detects closed, settled bursts on the trade
            # tape, measures them against pre-burst top-20, appends to
            # state.exec_events. Rolling medians per (side, bucket) are
            # published as sparse metric rows below — empty buckets are
            # silently omitted, not zero-filled.
            new_exec = detect_and_measure_bursts(state, now_ms)
            if new_exec:
                state.exec_events.extend(new_exec)
            # Burst Detection (PHASE 3A): standalone burst records over the
            # SAME shared burst boundaries exec-impact consumes. Append-only
            # to liquidity_bursts; OK rows per settled burst + one refusal
            # marker on transition into UNKNOWN/INSUFFICIENT/DROPPED.
            for rec in detect_bursts(state, now_ms):
                self._burst_buffer.append(rec.as_row())
            # Execution Validation (PHASE 3B): per-burst expected-vs-realized
            # over the SAME shared burst boundaries; refusal-first explicit
            # states. Append-only to liquidity_exec_validation.
            for ev in detect_exec_validation_records(state, now_ms):
                self._exec_val_buffer.append(ev.as_row())
            # PHASE 4A: burst-synchronized resiliency episodes over the SAME
            # shared burst boundaries; recovery tracked on the credible_depth
            # series (`depth`). Append-only; refusal-first; resiliency_score
            # (intelligence) left untouched.
            self._resiliency_buffer.extend(detect_resiliency(state, now_ms, depth))
            samples = (
                ("obi_rt", obi_rt(state)),
                ("credible_depth", depth),
                # Per-side decomposition + observable imbalance of the same
                # survivorship-filtered depth. Dense rows (emitted every
                # tick, None when mid is UNKNOWN) — same persistence
                # discipline as credible_depth; no interpolation.
                ("credible_bid_depth", bid_d),
                ("credible_ask_depth", ask_d),
                ("credible_depth_delta", credible_delta),
                # Measurement-quality of the credible_depth read above —
                # grades the snapshot sequence (freshness/coverage/gaps),
                # NOT the market. None = UNKNOWN/INSUFFICIENT (propagates);
                # additive diagnostic, nothing downstream consumes it.
                ("persistence_quality", persistence_quality(state, now_ms)),
                # `liq_stress` RESTORED: forceOrder is reachable via the
                # `/market` endpoint class (isolation-matrix verified); the
                # dedicated `_liquidation_loop` feeds state.liquidations for
                # tracked symbols. Sums forced-liquidation USD over the last
                # LIQ_WINDOW_MS; 0.0 when no liquidations in window.
                ("liq_stress", liquidation_stress_usd(state, now_ms)),
                ("resiliency_score", resiliency_score(state, now_ms)),
                ("recovery_time_ms", recovery_time_ms(state)),
                ("refill_velocity", refill_velocity_usd_per_s(state)),
                ("kyle_lambda", kyle_lambda(state, now_ms)),
                ("impact_score", impact_score(state, now_ms)),
                ("fragility_score", fragility_score(state, now_ms)),
            )
            for metric_name, value in samples:
                self._sample_buffer.append({
                    "symbol": symbol,
                    "metric": metric_name,
                    "ts": now_ms,
                    "value": value,
                    "price": mid,
                })
            # Sparse exec-impact rows: only non-empty (side, bucket)
            # combinations and the global exhaustion counter. We never
            # write a zero-filled placeholder for a bucket that had no
            # events in the window.
            for metric_name, value in rolling_exec_metrics(state, now_ms):
                self._sample_buffer.append({
                    "symbol": symbol,
                    "metric": metric_name,
                    "ts": now_ms,
                    "value": value,
                    "price": mid,
                })
        # Health probe: the sampler completed a pass.
        self._last_sample_ms = now_ms
        self._samples_total += 1

    def _write_status(self) -> None:
        """Persist current ws health into the single-row liquidity_ws_status
        table so the API can surface it to the frontend (live dot, stale
        indicator, reconnect badge)."""
        import json as _json
        from kazus_db.models import LiquidityWsStatus
        last_msg_dt = (
            datetime.fromtimestamp(self._last_message_at, tz=timezone.utc).replace(tzinfo=None)
            if self._last_message_at > 0
            else None
        )
        subscribed_json = _json.dumps(sorted(self.subscribed))
        with self.db_factory() as db:
            row = db.query(LiquidityWsStatus).filter(LiquidityWsStatus.id == 1).first()
            if row is None:
                row = LiquidityWsStatus(id=1)
                db.add(row)
            row.conn_id = self.ws.conn_id
            row.connected = self.ws.connected
            row.subscribed_json = subscribed_json
            row.last_message_at = last_msg_dt
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()

    async def _flush(self) -> int:
        if (not self._sample_buffer and not self._burst_buffer
                and not self._exec_val_buffer and not self._resiliency_buffer):
            return 0
        from kazus_db.models import (
            LiquidityBurst,
            LiquidityExecValidation,
            LiquidityResiliency,
            LiquiditySample,
        )
        batch = self._sample_buffer
        self._sample_buffer = []
        bursts = self._burst_buffer
        self._burst_buffer = []
        exec_vals = self._exec_val_buffer
        self._exec_val_buffer = []
        resiliency = self._resiliency_buffer
        self._resiliency_buffer = []
        # Health probe: mark flush in-flight (started > completed) so a stuck
        # DB write is observable as PERSISTENCE_BOTTLENECK. Completed/duration
        # are stamped only after commit returns.
        self._flush_started_ms = int(time.time() * 1000)
        with self.db_factory() as db:
            if batch:
                db.bulk_insert_mappings(LiquiditySample, batch)
            if bursts:
                db.bulk_insert_mappings(LiquidityBurst, bursts)
            if exec_vals:
                db.bulk_insert_mappings(LiquidityExecValidation, exec_vals)
            if resiliency:
                db.bulk_insert_mappings(LiquidityResiliency, resiliency)
            db.commit()
        done_ms = int(time.time() * 1000)
        self._flush_completed_ms = done_ms
        self._flush_duration_ms = float(done_ms - self._flush_started_ms)
        n = len(batch) + len(bursts) + len(exec_vals) + len(resiliency)
        self._flush_rows_total += n
        return n

    # ── runtime health (WS_RELIABILITY_001) ───────────────────────────────

    def _write_health(self, loop_lag_ms: float) -> None:
        """Append one diagnostic row localizing the runtime failure boundary.
        Pure read of the stage probes; classification is deterministic from the
        persisted numerics. Diagnostic-only — never feeds any metric."""
        from kazus_db.models import LiquidityRuntimeHealth
        row = health.build_health_row(
            now_ms=int(time.time() * 1000),
            loop_lag_ms=loop_lag_ms,
            subscribed_count=len(self.subscribed),
            conn_id=self.ws.conn_id,
            last_ws_message_ms=int(self._last_message_at * 1000),
            frames_total=self._frames_total,
            last_sample_ms=self._last_sample_ms,
            samples_total=self._samples_total,
            flush_started_ms=self._flush_started_ms,
            flush_completed_ms=self._flush_completed_ms,
            flush_duration_ms=self._flush_duration_ms,
            flush_rows_total=self._flush_rows_total,
        )
        with self.db_factory() as db:
            db.add(LiquidityRuntimeHealth(**row))
            db.commit()

    async def _health_loop(self, stop_event: asyncio.Event) -> None:
        """Fixed-cadence heartbeat. Measures event-loop lag (actual vs expected
        wake — the only direct scheduler-starvation signal) and appends a
        health row. Wrapped so a diagnostic failure can NEVER disrupt ingestion;
        not part of run()'s FIRST_COMPLETED set."""
        interval = health.HEALTH_INTERVAL_S
        prev = time.monotonic()
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            now_mono = time.monotonic()
            loop_lag_ms = max(0.0, (now_mono - prev) * 1000.0 - interval * 1000.0)
            prev = now_mono
            try:
                self._write_health(loop_lag_ms)
            except Exception as exc:  # noqa: BLE001
                logger.warning("runtime-health write failed: %s", exc)

    # ── liquidation feed (LIQ STRESS restoration) ──────────────────────────

    def _on_liquidation_frame(self, raw: str) -> None:
        """Parse one all-market forceOrder frame and feed state.liquidations
        for TRACKED symbols only. Feeds LIQ STRESS exclusively — liq_spike
        resiliency stays disabled via intelligence.LIQ_SPIKE_RESILIENCY_ENABLED."""
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        o = (msg.get("o") if isinstance(msg, dict) else None) or {}
        sym = (o.get("s") or "").upper()
        state = self.states.get(sym)
        if state is None:
            return  # all-market stream → keep only symbols we actually track
        try:
            price = float(o.get("ap") or o.get("p") or 0.0)   # avg fill price
            qty = float(o.get("z") or o.get("q") or 0.0)       # filled qty
            ts = int(o.get("T") or msg.get("E") or int(time.time() * 1000))
            side = str(o.get("S") or "")
        except (TypeError, ValueError):
            return
        if price > 0 and qty > 0:
            state.push_liquidation(Liquidation(ts=ts, side=side, price=price, qty=qty))

    async def _liquidation_loop(self, stop_event: asyncio.Event) -> None:
        """Always-on dedicated reader for the all-market forceOrder stream on the
        `/market` endpoint class (the only class that delivers it). Raw single
        stream — no SUBSCRIBE — so the main FuturesWsClient is untouched. Wrapped
        so a feed failure can NEVER disrupt ingestion; NOT in run()'s
        FIRST_COMPLETED set."""
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    FORCE_ORDER_WS_URL, ping_interval=20, ping_timeout=10,
                    close_timeout=5, max_size=2 * 1024 * 1024,
                ) as ws:
                    backoff = 1.0
                    logger.info("liquidation feed connected (%s)", FORCE_ORDER_WS_URL)
                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue
                        self._on_liquidation_frame(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning("liquidation feed error: %s", exc)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=min(30.0, backoff))
                    break
                except asyncio.TimeoutError:
                    backoff = min(30.0, backoff * 2)

    # ── main loop ─────────────────────────────────────────────────────────

    async def _reader_loop(self, stop_event: asyncio.Event) -> None:
        async for stream, data in self.ws.messages():
            if stop_event.is_set():
                return
            self._on_frame(stream, data)

    async def _ticker_loop(self, stop_event: asyncio.Event) -> None:
        last_reconcile = 0.0
        last_sample = 0.0
        last_flush = 0.0
        last_status = 0.0
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_reconcile >= RECONCILE_INTERVAL_S:
                await self._reconcile()
                last_reconcile = now
            if now - last_sample >= SAMPLE_INTERVAL_S and self.states:
                self._sample_all()
                last_sample = now
            if now - last_status >= STATUS_INTERVAL_S:
                try:
                    self._write_status()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ws status write failed: %s", exc)
                last_status = now
            if now - last_flush >= FLUSH_INTERVAL_S:
                try:
                    n = await self._flush()
                    if n:
                        logger.debug("realtime flush: %d rows", n)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("realtime flush failed: %s", exc)
                last_flush = now
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                break
            except asyncio.TimeoutError:
                pass
        # final flush so anything buffered doesn't get lost on shutdown.
        try:
            await self._flush()
        except Exception:  # noqa: BLE001
            pass

    async def run(self, stop_event: asyncio.Event) -> None:
        reader = asyncio.create_task(self._reader_loop(stop_event), name="ws-reader")
        ticker = asyncio.create_task(self._ticker_loop(stop_event), name="ws-ticker")
        # Diagnostic heartbeat (WS_RELIABILITY_001). Deliberately NOT in the
        # FIRST_COMPLETED set — if it ever dies, ingestion must continue.
        hb = asyncio.create_task(self._health_loop(stop_event), name="ws-health")
        # Dedicated all-market liquidation feed (LIQ STRESS). Like the heartbeat,
        # excluded from FIRST_COMPLETED — its failure must not stop ingestion.
        lq = asyncio.create_task(self._liquidation_loop(stop_event), name="ws-liquidation")
        try:
            await asyncio.wait([reader, ticker], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (reader, ticker, hb, lq):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await self.ws.close()
