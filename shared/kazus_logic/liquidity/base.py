"""Metric interface + shared context.

A `Metric` is a tiny pure object with a name, a display label, and a
compute() coroutine that takes a MetricContext and returns the current
value or None. Context bundles per-symbol upstream data (klines, depth
snapshot, mark price) so multiple metrics in the same polling cycle can
share an upstream fetch instead of re-hitting Binance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class OHLCV:
    """Single candle including volume — separate from setup-side Bar
    because the setup engine never reads volume and we don't want to
    touch that hot path. Liquidity owns its own bar type."""
    ts: int          # epoch ms (kline open time)
    open: float
    high: float
    low: float
    close: float
    volume: float    # base-asset volume


@dataclass
class DepthSnapshot:
    bids: list[tuple[float, float]]  # (price, qty), sorted desc by price
    asks: list[tuple[float, float]]  # (price, qty), sorted asc by price
    ts: int                          # epoch ms when fetched


@dataclass
class MetricContext:
    """Per-symbol bundle passed to every metric in a polling cycle.

    Fields are populated lazily by the worker before dispatching to
    metrics: klines on first kline-needing metric, depth on first
    depth-needing metric, etc. Metrics check which fields they need;
    if a needed field is None the metric returns None and the worker
    persists a NULL sample (the chart skips it).
    """
    symbol: str
    now_ts: int                                  # epoch ms of this cycle
    price: Optional[float] = None                # mark or last price
    h1_klines: Optional[list[OHLCV]] = None      # most-recent 100 H1 candles
    depth: Optional[DepthSnapshot] = None        # top-N depth snapshot


@dataclass
class MetricSample:
    ts: int
    value: Optional[float]
    price: Optional[float]


class Metric(Protocol):
    name: str                       # stable key, e.g. "atr_liquidity"
    label: str                      # display name, e.g. "ATR Liquidity"
    requires: tuple[str, ...]       # {"klines", "depth", "price"}

    async def compute(self, ctx: MetricContext) -> Optional[float]:
        ...
