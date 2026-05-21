"""ATR Liquidity = 24h volume / ATR(14, H1).

Reads volume and high/low/close off H1 klines. The metric is a measure
of "how much volume churns per unit of price range" — high values mean
the market absorbs a lot of trade flow without moving much, low values
mean thin / volatile / illiquid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..base import MetricContext

_ATR_PERIOD = 14
_VOLUME_WINDOW = 24  # last 24 H1 candles = 24h


def _true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _atr(klines, period: int) -> Optional[float]:
    if len(klines) < period + 1:
        return None
    # Wilder's smoothed ATR. Seed = simple mean of the first `period` TRs.
    trs = []
    for i in range(1, period + 1):
        trs.append(_true_range(klines[i].high, klines[i].low, klines[i - 1].close))
    atr = sum(trs) / period
    for i in range(period + 1, len(klines)):
        tr = _true_range(klines[i].high, klines[i].low, klines[i - 1].close)
        atr = (atr * (period - 1) + tr) / period
    return atr


@dataclass
class _AtrLiquidity:
    name: str = "atr_liquidity"
    label: str = "ATR Liquidity"
    requires: tuple[str, ...] = ("klines",)
    source: str = "rest"

    async def compute(self, ctx: MetricContext) -> Optional[float]:
        klines = ctx.h1_klines
        if not klines or len(klines) < _ATR_PERIOD + 1:
            return None
        atr = _atr(klines, _ATR_PERIOD)
        if atr is None or atr <= 0:
            return None
        window = klines[-_VOLUME_WINDOW:]
        volume = sum(b.volume for b in window)
        if volume <= 0:
            return None
        return volume / atr


ATR_LIQUIDITY = _AtrLiquidity()
