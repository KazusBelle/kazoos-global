"""Open Interest — USD-notional of all outstanding futures contracts.

Binance returns OI in base-asset contracts; we multiply by mark price so
the value is directly comparable across symbols of wildly different unit
prices (think SHIB vs BTC). Without the price scaling the column would
be dominated by tick-size, not actual positioning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..base import MetricContext


@dataclass
class _OpenInterest:
    name: str = "oi"
    label: str = "Open Interest (USD)"
    requires: tuple[str, ...] = ("open_interest", "price")
    source: str = "rest"

    async def compute(self, ctx: MetricContext) -> Optional[float]:
        if ctx.open_interest is None or ctx.price is None:
            return None
        try:
            return float(ctx.open_interest) * float(ctx.price)
        except (TypeError, ValueError):
            return None


OPEN_INTEREST = _OpenInterest()
