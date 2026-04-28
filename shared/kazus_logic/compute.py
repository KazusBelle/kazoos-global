"""
High-level 'compute snapshot for a symbol' helper that wires a Binance
kline fetch through the appropriate engine and returns both the Global
(D1) and Local (H1) results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .binance import BinanceFuturesClient
from .engine import (
    Bar,
    KazusGlobalEngine,
    KazusLocalEngine,
    ZoneResult,
)

# Length of the sparkline series we expose through the API.
SPARKLINE_LEN = 40

# Number of recent bars to scan for FVG detection.
FVG_LOOKBACK = 100


@dataclass
class SymbolSnapshot:
    symbol: str
    price: float
    global_result: ZoneResult
    local_result: ZoneResult
    global_trend: str        # "up" | "down" | "none"
    local_trend: str
    global_closes: List[float] = field(default_factory=list)
    local_closes: List[float] = field(default_factory=list)


def _trend_from_event(ev: Optional[str]) -> str:
    if ev in ("HH", "HL", "HH*"):
        return "up"
    if ev in ("LL", "LH", "LL*"):
        return "down"
    return "none"


def _find_nearest_bullish_fvg(bars: List[Bar], current_price: float) -> Optional[float]:
    """
    Scan the last FVG_LOOKBACK bars for bullish Fair Value Gaps.
    A bullish FVG: bars[i].low > bars[i-2].high
    Returns the bottom of the nearest unmitigated FVG to current_price, or None.
    """
    scan = bars[-FVG_LOOKBACK:] if len(bars) > FVG_LOOKBACK else bars
    fvgs: List[float] = []
    for i in range(2, len(scan)):
        if scan[i].low > scan[i - 2].high:
            fvg_low = scan[i - 2].high
            # Simple mitigation check: no bar after the gap closed below fvg_low
            mitigated = any(scan[j].close < fvg_low for j in range(i + 1, len(scan)))
            if not mitigated:
                fvgs.append(fvg_low)
    if not fvgs:
        return None
    return min(fvgs, key=lambda x: abs(x - current_price))


def _check_fvg_setup(zone_result: ZoneResult, confirmation_bars: List[Bar]) -> ZoneResult:
    """
    Override setup='yes' if price is in OTE and the last confirmation bar
    closed above the nearest bullish FVG from confirmation_bars.
    Returns a new ZoneResult (does not mutate in place).
    """
    if not zone_result.in_ote or not confirmation_bars:
        return zone_result

    current_price = confirmation_bars[-1].close
    nearest_fvg_low = _find_nearest_bullish_fvg(confirmation_bars, current_price)

    if nearest_fvg_low is None:
        return zone_result

    fvg_triggered = current_price >= nearest_fvg_low

    return ZoneResult(
        zone=zone_result.zone,
        in_ote=zone_result.in_ote,
        setup="yes" if fvg_triggered else "no",
        retracement=zone_result.retracement,
        direction=zone_result.direction,
        fib_low=zone_result.fib_low,
        fib_high=zone_result.fib_high,
        ote_low_price=zone_result.ote_low_price,
        ote_high_price=zone_result.ote_high_price,
    )


async def compute_symbol(
    client: BinanceFuturesClient,
    symbol: str,
    d1_limit: int = 500,
    h1_limit: int = 900,
    m15_limit: int = 600,
) -> SymbolSnapshot:
    d1_bars, h1_bars, m15_bars = await _fetch_all(
        client, symbol, d1_limit, h1_limit, m15_limit
    )

    # Drop the in-progress (last) bar — Binance returns it open.
    d1_closed = d1_bars[:-1] if len(d1_bars) > 1 else d1_bars
    h1_closed = h1_bars[:-1] if len(h1_bars) > 1 else h1_bars
    m15_closed = m15_bars[:-1] if len(m15_bars) > 1 else m15_bars

    g = KazusGlobalEngine()
    for bar in d1_closed:
        g.feed(bar)

    l = KazusLocalEngine()
    for bar in h1_closed:
        l.feed(bar)

    price = h1_bars[-1].close if h1_bars else 0.0

    global_result = g.snapshot(price)
    local_result = l.snapshot(price)

    # FVG-based setup detection:
    # Global (D1 OTE) confirmed by nearest H1 FVG
    global_result = _check_fvg_setup(global_result, h1_closed)
    # Local (H1 OTE) confirmed by nearest M15 FVG
    local_result = _check_fvg_setup(local_result, m15_closed)

    return SymbolSnapshot(
        symbol=symbol,
        price=price,
        global_result=global_result,
        local_result=local_result,
        global_trend=_trend_from_event(g.last_structure_event),
        local_trend=_trend_from_event(l.last_structure_event),
        global_closes=[b.close for b in d1_closed[-SPARKLINE_LEN:]],
        local_closes=[b.close for b in h1_closed[-SPARKLINE_LEN:]],
    )


async def _fetch_all(
    client: BinanceFuturesClient,
    symbol: str,
    d1_limit: int,
    h1_limit: int,
    m15_limit: int,
):
    import asyncio
    return await asyncio.gather(
        client.klines(symbol, "1d", limit=d1_limit),
        client.klines(symbol, "1h", limit=h1_limit),
        client.klines(symbol, "15m", limit=m15_limit),
    )
