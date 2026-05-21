"""OBI = (sum_bid_qty - sum_ask_qty) / (sum_bid_qty + sum_ask_qty).

Order Book Imbalance over the top 20 levels of the depth snapshot.
Range: [-1, +1]. Positive → more bid-side liquidity (buy-pressure
support), negative → more ask-side (sell-side overhang).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..base import MetricContext

_LEVELS = 20


@dataclass
class _Obi:
    name: str = "obi"
    label: str = "OBI"
    requires: tuple[str, ...] = ("depth",)

    async def compute(self, ctx: MetricContext) -> Optional[float]:
        depth = ctx.depth
        if depth is None or not depth.bids or not depth.asks:
            return None
        bid_qty = sum(q for _, q in depth.bids[:_LEVELS])
        ask_qty = sum(q for _, q in depth.asks[:_LEVELS])
        total = bid_qty + ask_qty
        if total <= 0:
            return None
        return (bid_qty - ask_qty) / total


OBI = _Obi()
