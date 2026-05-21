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

    def apply_depth20(self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]], now_ms: int) -> None:
        """Replace the top-20 with the new snapshot. Levels whose qty
        is unchanged from the prior snapshot keep their first_seen_ts;
        new or changed levels get first_seen_ts = now."""
        self.bids = _merge_levels(self.bids, bids, now_ms)
        self.asks = _merge_levels(self.asks, asks, now_ms)
        self.last_depth_ts = now_ms

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
