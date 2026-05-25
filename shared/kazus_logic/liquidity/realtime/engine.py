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
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Set

from .exec_impact import detect_and_measure_bursts, rolling_exec_metrics
from .intelligence import (
    fragility_score,
    impact_score,
    kyle_lambda,
    recovery_time_ms,
    refill_velocity_usd_per_s,
    resiliency_score,
    update_intelligence,
)
from .metrics import credible_depth_usd, obi_rt
from .orderbook import SymbolState, Trade
from .ws_client import FuturesWsClient

logger = logging.getLogger("kazus.liquidity.realtime")

_STREAM_SUFFIXES = ("depth20@100ms", "bookTicker", "trade")

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
        self._last_message_at: float = 0.0  # epoch seconds of latest frame

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
            depth = credible_depth_usd(state, now_ms)
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
            samples = (
                ("obi_rt", obi_rt(state)),
                ("credible_depth", depth),
                # `liq_stress` dropped: `<s>@forceOrder` is unavailable
                # from this network perimeter (verified at the wire),
                # so the metric had no input and was writing constant
                # 0.0 — silently false. Surface is dropped at the
                # registry level so the chart never claims emptiness.
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
        if not self._sample_buffer:
            return 0
        from kazus_db.models import LiquiditySample
        batch = self._sample_buffer
        self._sample_buffer = []
        with self.db_factory() as db:
            db.bulk_insert_mappings(LiquiditySample, batch)
            db.commit()
        return len(batch)

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
        try:
            await asyncio.wait([reader, ticker], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (reader, ticker):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            await self.ws.close()
