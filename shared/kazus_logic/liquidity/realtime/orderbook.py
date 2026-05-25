"""Per-symbol live state populated from WS streams.

The orderbook side of state uses `<symbol>@depth20@100ms` — a fresh
top-20 snapshot every 100ms — instead of the diff stream. That avoids
the lastUpdateId / REST-snapshot resync dance and is enough depth for
the metrics we currently compute (Credible Depth, realtime OBI).

For each price level we track `first_seen_ts`: when the (price, qty)
pair was first observed. The timestamp resets whenever qty changes —
because the new quantity itself is "fresh" liquidity that has not yet
demonstrated persistence. Credible Depth filters by min-age against
this field to defeat spoofing / fleeting orders.

Trade tape and liquidation tape are bounded deques retaining only the
last `_TAPE_WINDOW_MS` of events — enough for our rolling windows.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


_TAPE_WINDOW_MS = 5 * 60 * 1000  # keep 5 min of trades / liquidations
_DEPTH_HISTORY_WINDOW_MS = 5 * 60 * 1000  # 5-min rolling for depth/spread
_RECOVERY_TIMEOUT_MS = 90_000             # give up tracking after this
# Short ring of full top-N snapshots for the exec-impact layer. It needs
# pre-burst and post-settle book states; 5s of 100ms-cadence frames is
# enough to bracket any same-side burst we measure (BURST_GAP_MS=250,
# SETTLE_MS=500). Bounded by count so a stalled stream can't grow it.
_BOOK_HISTORY_MAX = 60
_EXEC_EVENT_WINDOW_MS = 5 * 60 * 1000


@dataclass
class Trade:
    ts: int          # epoch ms
    price: float
    qty: float
    is_buyer_maker: bool  # True → trade was a taker SELL (price went down)


@dataclass
class Liquidation:
    ts: int
    side: str        # "BUY" → short was liquidated (forced buy)
                     # "SELL" → long was liquidated (forced sell)
    price: float
    qty: float


@dataclass
class DepthSample:
    """One sampled snapshot of microstructure state. Populated by the
    realtime sampler at ~1Hz and used by intelligence metrics (resiliency,
    Kyle Lambda) that need a short rolling history."""
    ts: int
    depth_usd: Optional[float]   # credible depth at sample time
    spread_bps: Optional[float]  # spread in basis points


@dataclass(frozen=True)
class BookSnapshot:
    """Frozen top-N book state captured each depth20 frame.

    Held in a short ring (SymbolState.book_history) so the exec-impact
    layer can locate pre-burst and post-settle book states by timestamp.
    `bids` is sorted descending by price, `asks` ascending. `mid` is
    derived from this same snapshot (not bookTicker) so book-walk and
    mid are self-consistent.
    """
    ts: int
    bids: Tuple[Tuple[float, float], ...]  # ((price, qty), ...) desc
    asks: Tuple[Tuple[float, float], ...]  # ((price, qty), ...) asc
    mid: float


@dataclass
class RecoveryEvent:
    """A microstructure event we're measuring recovery from.

    `pre_depth` is the credible depth snapshot taken just before the
    detector tripped — the symbol is considered "recovered" once depth
    climbs back to RECOVERY_FRACTION × pre_depth. `recovery_ms` and
    `refill_velocity` are filled once recovery happens (or NULL if the
    event ages out without recovering).
    """
    kind: str                        # "liq_spike" | "spread_explosion" | "depth_collapse" | "obi_flip"
    started_ts: int
    pre_depth: Optional[float]
    recovered_ts: Optional[int] = None
    recovery_ms: Optional[int] = None
    refill_velocity: Optional[float] = None  # depth-USD per second


@dataclass
class SymbolState:
    symbol: str
    # bids/asks: price → (qty, first_seen_ts_ms)
    bids: Dict[float, Tuple[float, int]] = field(default_factory=dict)
    asks: Dict[float, Tuple[float, int]] = field(default_factory=dict)
    last_depth_ts: Optional[int] = None
    # best bid/ask from bookTicker (sub-100ms updates)
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    best_ts: Optional[int] = None
    # trade & liquidation tapes
    trades: Deque[Trade] = field(default_factory=deque)
    liquidations: Deque[Liquidation] = field(default_factory=deque)
    # rolling microstructure samples, populated by the realtime sampler
    depth_history: Deque[DepthSample] = field(default_factory=deque)
    # in-flight and completed recovery events — bounded; resiliency_score
    # consumes the most-recent completed one.
    events: Deque[RecoveryEvent] = field(default_factory=deque)
    # bookkeeping for event-detector debouncing
    last_event_ts: int = 0
    # Short ring of full top-N snapshots — consumed by the exec-impact
    # layer to locate pre-burst and post-settle book states. Pushed each
    # depth20 frame; bounded by _BOOK_HISTORY_MAX.
    book_history: Deque["BookSnapshot"] = field(default_factory=deque)
    # Forward-only exec-impact events (ExecEvent from exec_impact.py).
    # Pruned to last _EXEC_EVENT_WINDOW_MS.
    exec_events: Deque[object] = field(default_factory=deque)
    # Timestamp through which the burst-detector has already scanned the
    # trade tape. Trades with ts <= cursor are never re-considered.
    exec_cursor_ts: int = 0

    def apply_depth20(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]], now_ms: int) -> None:
        """Replace the top-20 with the new snapshot. Levels whose qty
        is unchanged from the prior snapshot keep their first_seen_ts;
        new or changed levels get first_seen_ts = now.

        Also pushes a frozen BookSnapshot into book_history so the
        exec-impact layer can find pre/post states by timestamp without
        racing the live mutable dicts.
        """
        self.bids = _merge_levels(self.bids, bids, now_ms)
        self.asks = _merge_levels(self.asks, asks, now_ms)
        self.last_depth_ts = now_ms
        if self.bids and self.asks:
            bids_sorted = tuple(sorted(
                ((p, q) for p, (q, _) in self.bids.items()),
                key=lambda x: -x[0],
            ))
            asks_sorted = tuple(sorted(
                ((p, q) for p, (q, _) in self.asks.items()),
                key=lambda x: x[0],
            ))
            mid = (bids_sorted[0][0] + asks_sorted[0][0]) / 2
            self.book_history.append(BookSnapshot(
                ts=now_ms, bids=bids_sorted, asks=asks_sorted, mid=mid,
            ))
            while len(self.book_history) > _BOOK_HISTORY_MAX:
                self.book_history.popleft()

    def apply_book_ticker(self, best_bid: float, best_ask: float, ts_ms: int) -> None:
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.best_ts = ts_ms

    def push_trade(self, t: Trade) -> None:
        self.trades.append(t)
        self._prune_tape(self.trades, t.ts)

    def push_liquidation(self, liq: Liquidation) -> None:
        self.liquidations.append(liq)
        self._prune_tape(self.liquidations, liq.ts)

    @staticmethod
    def _prune_tape(d: Deque, now_ms: int) -> None:
        cutoff = now_ms - _TAPE_WINDOW_MS
        while d and d[0].ts < cutoff:
            d.popleft()

    def push_depth_sample(self, sample: DepthSample, max_events: int = 20) -> None:
        self.depth_history.append(sample)
        # Prune old samples
        cutoff = sample.ts - _DEPTH_HISTORY_WINDOW_MS
        while self.depth_history and self.depth_history[0].ts < cutoff:
            self.depth_history.popleft()
        # Keep the events deque bounded so a long session doesn't grow it forever.
        while len(self.events) > max_events:
            self.events.popleft()

    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        if self.bids and self.asks:
            top_bid = max(self.bids.keys())
            top_ask = min(self.asks.keys())
            return (top_bid + top_ask) / 2
        return None


def _merge_levels(
    prev: Dict[float, Tuple[float, int]],
    new_levels: List[Tuple[float, float]],
    now_ms: int,
) -> Dict[float, Tuple[float, int]]:
    out: Dict[float, Tuple[float, int]] = {}
    for price, qty in new_levels:
        if qty <= 0:
            continue
        prior = prev.get(price)
        if prior is not None and prior[0] == qty:
            out[price] = prior  # carry forward — level unchanged, age accumulates
        else:
            out[price] = (qty, now_ms)
    return out
