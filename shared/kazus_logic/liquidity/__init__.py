"""Liquidity metrics framework.

Every metric is its own module, registered in REGISTRY by name. Some
metrics are computed from REST-polled snapshots (source="rest") on a
60s cadence; others from a per-symbol live WS state (source="ws") at
~1Hz. The REST poller and the realtime sampler filter REGISTRY by
source to know which metrics each owns.

Realtime metric DESCRIPTORS live here too — but their actual compute
functions live in realtime/metrics.py because they read SymbolState,
not the generic MetricContext. The descriptors below are stubs whose
compute() always returns None; they exist so /api/liquidity/metrics
surfaces them in the UI metric-tab list with the proper labels.
"""

from dataclasses import dataclass
from typing import Optional

from .base import Metric, MetricContext, MetricSample
from .metrics.atr_liquidity import ATR_LIQUIDITY
from .metrics.spread import SPREAD
from .metrics.obi import OBI


@dataclass
class _WsMetricDescriptor:
    """A placeholder for a WS-sourced metric. The real compute lives in
    realtime/metrics.py; this descriptor is just enough metadata for the
    REGISTRY, the API and the REST poller (which uses source to skip
    these)."""
    name: str
    label: str
    requires: tuple = ()
    source: str = "ws"

    async def compute(self, ctx: MetricContext) -> Optional[float]:
        return None  # never called — sampler uses realtime.metrics


OBI_RT = _WsMetricDescriptor(name="obi_rt", label="Realtime OBI")
CREDIBLE_DEPTH = _WsMetricDescriptor(name="credible_depth", label="Credible Depth")
LIQ_STRESS = _WsMetricDescriptor(name="liq_stress", label="Liquidation Stress")


REGISTRY: dict[str, Metric] = {
    m.name: m
    for m in (ATR_LIQUIDITY, SPREAD, OBI, OBI_RT, CREDIBLE_DEPTH, LIQ_STRESS)
}

__all__ = [
    "Metric",
    "MetricContext",
    "MetricSample",
    "REGISTRY",
    "OBI_RT",
    "CREDIBLE_DEPTH",
    "LIQ_STRESS",
]
