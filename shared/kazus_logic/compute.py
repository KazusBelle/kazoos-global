"""
High-level 'compute snapshot for a symbol' helper that wires a Binance
kline fetch through the appropriate engine and returns both the Global
(D1) and Local (H1) results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .binance import BinanceFuturesClient
from .engine import (
    KazusGlobalEngine,
    KazusLocalEngine,
    ZoneResult,
)


@dataclass
class SymbolSnapshot:
    symbol: str
    price: float
    global_result: ZoneResult
    local_result: ZoneResult
    global_trend: str        # "up" | "down" | "none"  (last structure direction)
    local_trend: str


def _trend_from_event(ev: Optional[str]) -> str:
    if ev in ("HH", "HL", "HH*"):
        return "up"
    if ev in ("LL", "LH", "LL*"):
        return "down"
    return "none"


async def compute_symbol(
    client: BinanceFuturesClient, symbol: str, d1_limit: int = 500, h1_limit: int = 900
) -> SymbolSnapshot:
    d1_bars = await client.klines(symbol, "1d", limit=d1_limit)
    h1_bars = await client.klines(symbol, "1h", limit=h1_limit)

    # drop the last bar of each series if it is not closed yet. Binance
    # returns the in-progress bar last; Pine operates on closed HTF bars.
    if len(d1_bars) > 1:
        d1_closed = d1_bars[:-1]
    else:
        d1_closed = d1_bars
    if len(h1_bars) > 1:
        h1_closed = h1_bars[:-1]
    else:
        h1_closed = h1_bars

    g = KazusGlobalEngine()
    for bar in d1_closed:
        g.feed(bar)

    l = KazusLocalEngine()
    for bar in h1_closed:
        l.feed(bar)

    price = h1_bars[-1].close if h1_bars else 0.0

    return SymbolSnapshot(
        symbol=symbol,
        price=price,
        global_result=g.snapshot(price),
        local_result=l.snapshot(price),
        global_trend=_trend_from_event(g.last_structure_event),
        local_trend=_trend_from_event(l.last_structure_event),
    )
