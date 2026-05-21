"""Liquidity-specific Binance Futures REST fetchers.

Kept separate from shared/kazus_logic/binance.py because the setup
engine's Bar type intentionally excludes volume — we don't want the
liquidity loop importing into the setup hot path or vice versa.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx

from .base import DepthSnapshot, OHLCV

logger = logging.getLogger("kazus.liquidity.fetch")

FUTURES_BASE = "https://fapi.binance.com"


async def fetch_h1_klines(
    client: httpx.AsyncClient, symbol: str, limit: int = 100
) -> Optional[List[OHLCV]]:
    """Last `limit` closed H1 candles. The current (open) candle is
    dropped — we only sample completed data so two consecutive polling
    cycles compute the same ATR for the same closed window."""
    try:
        r = await client.get(
            f"{FUTURES_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1h", "limit": limit + 1},
        )
        r.raise_for_status()
        rows = r.json()
    except httpx.HTTPError as exc:
        logger.warning("klines fetch failed for %s: %s", symbol, exc)
        return None
    if not rows:
        return None
    # Last row may be the still-open candle; trim if its close time is in
    # the future (Binance returns it eagerly). We compare against open
    # time + interval to keep it cheap.
    bars: list[OHLCV] = []
    for row in rows[:-1]:  # last is open candle; drop unconditionally
        bars.append(
            OHLCV(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return bars


async def fetch_depth(
    client: httpx.AsyncClient, symbol: str, limit: int = 20
) -> Optional[DepthSnapshot]:
    """Top-N depth snapshot. Binance allows limit ∈ {5, 10, 20, 50,
    100, 500, 1000} for /fapi/v1/depth — 20 is enough for Spread + OBI."""
    try:
        r = await client.get(
            f"{FUTURES_BASE}/fapi/v1/depth",
            params={"symbol": symbol, "limit": limit},
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        logger.warning("depth fetch failed for %s: %s", symbol, exc)
        return None
    bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
    ts = int(data.get("E") or data.get("T") or 0)
    return DepthSnapshot(bids=bids, asks=asks, ts=ts)
