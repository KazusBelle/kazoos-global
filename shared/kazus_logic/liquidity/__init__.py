"""Liquidity metrics framework.

Every metric is its own module, registered in REGISTRY by name. The
polling worker iterates the registry per symbol and writes one
LiquiditySample row per (symbol, metric) per cycle. New metrics are
added by dropping a file in metrics/ and registering it here — no
schema migration, no worker changes.
"""

from .base import Metric, MetricContext, MetricSample
from .metrics.atr_liquidity import ATR_LIQUIDITY
from .metrics.spread import SPREAD
from .metrics.obi import OBI

REGISTRY: dict[str, Metric] = {
    m.name: m
    for m in (ATR_LIQUIDITY, SPREAD, OBI)
}

__all__ = ["Metric", "MetricContext", "MetricSample", "REGISTRY"]
