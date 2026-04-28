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

    closed = bars[:-1] if len(bars) > 1 else bars
    for b in closed:
        engine.feed(Bar(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close))

    # Structure timeline. The engines already collapse HH*/LL* repeats inside
    # one leg, but we still de-dupe defensively (multiple writes can land on
    # the same bar+label when a trend change confirms a pending event).
    swings: List[SwingPoint] = []
    seen_keys: set = set()
    for bar_index, price, label in engine.structure_events:
        if not (0 <= bar_index < len(closed)):
            continue
        key = (bar_index, label)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        swings.append(
            SwingPoint(ts=closed[bar_index].ts, price=price, label=label)
        )

    # FVG boxes — extend each gap to the right edge of the closed-bar window.
    # The frontend draws them as semi-transparent rectangles spanning that range.
    fvgs: List[FvgBox] = []
    if closed:
        end_ts = closed[-1].ts
        for bar_index, top, bottom, kind in engine.fvg_events:
            if not (0 <= bar_index < len(closed)):
                continue
            fvgs.append(
                FvgBox(
                    ts=closed[bar_index].ts,
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
_INTERVAL_DEFAULT_LIMIT = {"1d": 500, "1h": 900, "15m": 600}


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
    try:
        bars = await _fetch_klines(symbol.upper(), interval, effective_limit)
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
