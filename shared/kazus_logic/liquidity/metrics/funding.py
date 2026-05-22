"""Current funding rate (fraction per 8h period).

Sourced from /fapi/v1/premiumIndex.lastFundingRate. Funding is settled
every 8h on Binance USDT-M, but the "last" rate is always available and
moves continuously as the market expectation drifts — so polling at the
liquidity cadence still produces a useful time-series.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..base import MetricContext


@dataclass
class _Funding:
    name: str = "funding"
    label: str = "Funding Rate"
    requires: tuple[str, ...] = ("funding_rate",)
    source: str = "rest"

    async def compute(self, ctx: MetricContext) -> Optional[float]:
        if ctx.funding_rate is None:
            return None
        try:
            return float(ctx.funding_rate)
        except (TypeError, ValueError):
            return None


FUNDING = _Funding()
