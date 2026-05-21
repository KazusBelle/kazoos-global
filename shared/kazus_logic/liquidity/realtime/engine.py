"""Top-level realtime engine.

Owns one FuturesWsClient. Periodically reads the desired-subscription
set from `liquidity_active_subs` (managed via the heartbeat HTTP
endpoint), reconciles WS subscriptions, dispatches incoming frames to
per-symbol SymbolState, and samples metrics into liquidity_samples at
1Hz with batched writes.

Streams subscribed per active symbol:
  <s>@depth20@100ms   — orderbook top-20 every 100ms (Credible Depth, OBI)
  <s>@bookTicker      — best bid/ask, sub-100ms (mid_price)
  <s>@aggTrade        — trades tape (kept for future Kyle Lambda)
  <s>@forceOrder      — liquidations (Liquidation Stress)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Set, Tuple

from .metrics import credible_depth_usd, liquidation_stress_usd, obi_rt
from .orderbook import Liquidation, SymbolState, Trade
from .ws_client import FuturesWsClient

logger = logging.getLogger("kazus.liquidity.realtime")

_STREAM_SUFFIXES = ("depth20@100ms", "bookTicker", "aggTrade", "forceOrder")

RECONCILE_INTERVAL_S = 5
SAMPLE_INTERVAL_S = 1.0
FLUSH_INTERVAL_S = 5.0


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

    # ── desired-set reconciliation ────────────────────────────────────────

    async def _read_desired(self) -> Set[str]:
        """Return the symbols that should currently be live-subscribed,
        per the liquidity_active_subs table. Rows whose expires_at is in
        the past are ignored (the row is left in place — its next write
        will UPSERT a fresh expires_at)."""
        from kazus_db.models import LiquidityActiveSub
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.db_factory() as db:
            rows = (
                db.query(LiquidityActiveSub)
                .filter(LiquidityActiveSub.expires_at > now_naive)
                .all()
            )
            return {r.symbol.upper() for r in rows}

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
            elif suffix == "aggTrade":
                ts = int(data.get("E") or data.get("T") or now_ms)
                price = float(data.get("p") or 0.0)
                qty = float(data.get("q") or 0.0)
                is_buyer_maker = bool(data.get("m", False))
                if price > 0 and qty > 0:
                    state.push_trade(Trade(ts=ts, price=price, qty=qty, is_buyer_maker=is_buyer_maker))
            elif suffix == "forceOrder":
                # frame: {"o": {"s","S","p","q","T",...}}
                o = data.get("o") or {}
                ts = int(o.get("T") or now_ms)
                side = str(o.get("S") or "").upper()
                price = float(o.get("ap") or o.get("p") or 0.0)
                qty = float(o.get("q") or 0.0)
                if price > 0 and qty > 0 and side in ("BUY", "SELL"):
                    state.push_liquidation(Liquidation(ts=ts, side=side, price=price, qty=qty))
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("frame parse failed for %s: %s", stream, exc)

    # ── sampling + flush ──────────────────────────────────────────────────

    def _sample_all(self) -> None:
        """One round of metric snapshots for every subscribed symbol.
        Results go into self._sample_buffer; the flusher drains it."""
        now_ms = int(time.time() * 1000)
        for symbol, state in self.states.items():
            mid = state.mid_price()
            samples = (
                ("obi_rt", obi_rt(state)),
                ("credible_depth", credible_depth_usd(state, now_ms)),
                ("liq_stress", liquidation_stress_usd(state, now_ms)),
            )
            for metric_name, value in samples:
                self._sample_buffer.append({
                    "symbol": symbol,
                    "metric": metric_name,
                    "ts": now_ms,
                    "value": value,
                    "price": mid,
                })

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
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_reconcile >= RECONCILE_INTERVAL_S:
                await self._reconcile()
                last_reconcile = now
            if now - last_sample >= SAMPLE_INTERVAL_S and self.states:
                self._sample_all()
                last_sample = now
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
