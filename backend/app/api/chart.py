"""
Chart endpoint — returns OHLCV bars + structure events + FVG boxes + active
fibonacci anchors for a given symbol/timeframe. Mirrors the Pine indicators
in `docs/pine/`. Used by the frontend candlestick chart modal.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from kazus_logic.engine import (
    Bar,
    KazusGlobalEngine,
    KazusLocalEngine,
)

from ..core.cache import TTLCache
from .deps import get_current_user

router = APIRouter(tags=["chart"])

FUTURES_BASE = "https://fapi.binance.com"

ALLOWED_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


class OHLCVBar(BaseModel):
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class SwingPoint(BaseModel):
    ts: int
    price: float
    label: str  # HH | HL | LL | LH


class FvgBox(BaseModel):
    ts: int       # timestamp of the originating bar (the bar two-back, i.e. the gap's left edge)
    end_ts: int   # right edge timestamp (last closed bar)
    top: float
    bottom: float
    kind: str     # bullish | bearish


class ChartResponse(BaseModel):
    symbol: str
    interval: str
    bars: List[OHLCVBar]
    swings: List[SwingPoint] = []
    fvgs: List[FvgBox] = []
    fib_high: Optional[float] = None
    fib_low: Optional[float] = None
    fib_direction: str = "none"


async def _fetch_klines(symbol: str, interval: str, limit: int) -> List[OHLCVBar]:
    async with httpx.AsyncClient(base_url=FUTURES_BASE, timeout=15.0) as client:
        r = await client.get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        r.raise_for_status()
        raw = r.json()
    return [
        OHLCVBar(
            ts=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in raw
    ]


def _analyze(
    bars: List[OHLCVBar], interval: str
) -> Tuple[List[SwingPoint], List[FvgBox], Optional[float], Optional[float], str]:
    """Run the matching engine over closed bars and pull out the structure
    timeline, FVG boxes, and active fib state."""
    if interval == "1d":
        engine = KazusGlobalEngine()
    elif interval == "1h":
        engine = KazusLocalEngine()
    else:
        return [], [], None, None, "none"

    # The chart modal should visually match TradingView, which includes the
    # currently forming candle and the live/potential structure it can imply.
    # Screener + alerts still use closed-bar compute in shared/kazus_logic.
    chart_bars = list(bars)
    for b in chart_bars:
        engine.feed(Bar(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close))

    # Structure timeline. The engines already collapse repeated swing writes
    # inside one leg, but we still de-dupe defensively (multiple writes can
    # land on the same bar+label when a trend change confirms a pending event).
    swings: List[SwingPoint] = []
    seen_keys: set = set()
    for bar_index, price, label in engine.structure_events:
        if not (0 <= bar_index < len(chart_bars)):
            continue
        key = (bar_index, label)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        swings.append(
            SwingPoint(ts=chart_bars[bar_index].ts, price=price, label=label)
        )

    # Global/D1 chart parity with TradingView: when the active bullish fib has
    # already extended above the last confirmed high pivot (or the active
    # bearish fib has already extended below the last confirmed low pivot),
    # Pine shows a provisional HH/LL on the live leg. The engine keeps the fib
    # state, but does not surface that provisional label in `structure_events`,
    # so the chart would otherwise look like the move was "missed".
    if interval == "1d":
        if engine.fib_state.direction == "bullish":
            potential_idx = getattr(engine, "tracked_fib_high_index", None)
            potential_price = getattr(engine, "tracked_fib_high", None)
            last_high = getattr(engine, "last_swing_high", None)
            if (
                potential_idx is not None
                and potential_price is not None
                and last_high is not None
                and potential_price > last_high
                and 0 <= potential_idx < len(chart_bars)
            ):
                key = (potential_idx, "HH")
                if key not in seen_keys:
                    seen_keys.add(key)
                    swings.append(
                        SwingPoint(
                            ts=chart_bars[potential_idx].ts,
                            price=potential_price,
                            label="HH",
                        )
                    )
        elif engine.fib_state.direction == "bearish":
            potential_idx = getattr(engine, "tracked_fib_low_index", None)
            potential_price = getattr(engine, "tracked_fib_low", None)
            last_low = getattr(engine, "last_swing_low", None)
            if (
                potential_idx is not None
                and potential_price is not None
                and last_low is not None
                and potential_price < last_low
                and 0 <= potential_idx < len(chart_bars)
            ):
                key = (potential_idx, "LL")
                if key not in seen_keys:
                    seen_keys.add(key)
                    swings.append(
                        SwingPoint(
                            ts=chart_bars[potential_idx].ts,
                            price=potential_price,
                            label="LL",
                        )
                    )

    # FVG boxes — extend each gap to the right edge of the closed-bar window.
    # The frontend draws them as semi-transparent rectangles spanning that range.
    fvgs: List[FvgBox] = []
    if chart_bars:
        end_ts = chart_bars[-1].ts
        for bar_index, top, bottom, kind in engine.fvg_events:
            if not (0 <= bar_index < len(chart_bars)):
                continue
            fvgs.append(
                FvgBox(
                    ts=chart_bars[bar_index].ts,
                    end_ts=end_ts,
                    top=top,
                    bottom=bottom,
                    kind=kind,
                )
            )

    # Normalize the active fib so the chart can compute retracement uniformly.
    fib_state = engine.fib_state
    if fib_state.direction == "bullish":
        return swings, fvgs, fib_state.fib_high, fib_state.swing_low, "bullish"
    if fib_state.direction == "bearish":
        return swings, fvgs, fib_state.swing_high, fib_state.fib_low, "bearish"
    return swings, fvgs, None, None, "none"


# Per-interval defaults match shared/kazus_logic/compute.py worker so the
# chart engine state matches what the screener table shows.
_INTERVAL_DEFAULT_LIMIT = {"1d": 500, "1h": 900, "15m": 600, "5m": 600}

# 10s: long enough that panning between symbols and re-opening a chart stops
# generating outbound traffic, short enough that the forming bar still looks
# live. Every timeframe served here is 1m or slower, so a hit can never be
# more than a fraction of a bar behind.
_KLINES_TTL_S = 10.0
_klines_cache: TTLCache[list] = TTLCache(ttl=_KLINES_TTL_S)


@router.get("/chart/{symbol}", response_model=ChartResponse)
async def get_chart(
    symbol: str,
    interval: str = Query("1h", description="Kline interval"),
    limit: Optional[int] = Query(None, ge=50, le=1500),
    _=Depends(get_current_user),
):
    if interval not in ALLOWED_INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(ALLOWED_INTERVALS)}")
    effective_limit = limit if limit is not None else _INTERVAL_DEFAULT_LIMIT.get(interval, 500)
    sym = symbol.upper()
    try:
        # Cached for _KLINES_TTL_S. The newest bar is still forming, so a hit
        # can be up to that many seconds behind — invisible on 1h/4h/1d, and
        # the alternative was an outbound Binance request on every chart open.
        bars = await _klines_cache.get(
            f"{sym}:{interval}:{effective_limit}",
            lambda: _fetch_klines(sym, interval, effective_limit),
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Binance error: {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    swings, fvgs, fib_high, fib_low, fib_direction = _analyze(bars, interval)

    return ChartResponse(
        symbol=symbol.upper(),
        interval=interval,
        bars=bars,
        swings=swings,
        fvgs=fvgs,
        fib_high=fib_high,
        fib_low=fib_low,
        fib_direction=fib_direction,
    )
