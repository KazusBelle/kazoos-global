"""Spread = (best_ask - best_bid) / mid_price.

Reads the top of the depth snapshot. Result is in fractional units
(e.g. 0.0001 = 1 bps). Tighter is more liquid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..base import MetricContext


@dataclass
class _Spread:
    name: str = "spread"
    label: str = "Spread"
    requires: tuple[str, ...] = ("depth",)
    source: str = "rest"

    async def compute(self, ctx: MetricContext) -> Optional[float]:
        depth = ctx.depth
        if depth is None or not depth.bids or not depth.asks:
            return None
        best_bid = depth.bids[0][0]
        best_ask = depth.asks[0][0]
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            return None
        mid = (best_bid + best_ask) / 2
        return (best_ask - best_bid) / mid


SPREAD = _Spread()
