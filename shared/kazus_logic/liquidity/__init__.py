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
from .metrics.derived import (
    DERIVED,
    FUNDING_Z,
    OI_DELTA_1H,
    OI_DELTA_5M,
    OI_DELTA_24H,
)
from .metrics.funding import FUNDING
from .metrics.obi import OBI
from .metrics.open_interest import OPEN_INTEREST
from .metrics.spread import SPREAD


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
# `liq_stress` is kept in the registry so downstream research.py +
# liquidityIntelligence.ts consumers don't break, but the realtime
# engine no longer writes samples for it (verified 2026-05-25:
# `<s>@forceOrder` is unavailable on this network — Binance accepts
# SUBSCRIBE silently, then delivers zero frames; `!forceOrder@arr` is
# likewise empty). The chart renders "No data in this window yet"
# instead of a fabricated constant 0.0. Re-enable sampling when the
# forceOrder feed becomes available again.
LIQ_STRESS = _WsMetricDescriptor(name="liq_stress", label="Liquidation Stress")
# Phase-4 intelligence layer — WS-only. Resiliency tracks recovery from
# microstructure events; Kyle Lambda measures price impact per signed
# flow over a rolling tape window.
RESILIENCY_SCORE = _WsMetricDescriptor(name="resiliency_score", label="Resiliency Score")
RECOVERY_TIME_MS = _WsMetricDescriptor(name="recovery_time_ms", label="Recovery Time (ms)")
REFILL_VELOCITY = _WsMetricDescriptor(name="refill_velocity", label="Refill Velocity ($/s)")
KYLE_LAMBDA = _WsMetricDescriptor(name="kyle_lambda", label="Kyle Lambda")
IMPACT_SCORE = _WsMetricDescriptor(name="impact_score", label="Impact Score")
FRAGILITY_SCORE = _WsMetricDescriptor(name="fragility_score", label="Fragility Score")


REGISTRY: dict[str, Metric] = {
    m.name: m
    for m in (
        ATR_LIQUIDITY,
        SPREAD,
        OBI,
        OBI_RT,
        CREDIBLE_DEPTH,
        LIQ_STRESS,
        OPEN_INTEREST,
        FUNDING,
        *DERIVED,  # oi_delta_5m/1h/24h + funding_z
        # Phase-4 intelligence layer (WS-only).
        RESILIENCY_SCORE,
        RECOVERY_TIME_MS,
        REFILL_VELOCITY,
        KYLE_LAMBDA,
        IMPACT_SCORE,
        FRAGILITY_SCORE,
    )
}

__all__ = [
    "Metric",
    "MetricContext",
    "MetricSample",
    "REGISTRY",
    "OBI_RT",
    "CREDIBLE_DEPTH",
    "LIQ_STRESS",
    "OPEN_INTEREST",
    "FUNDING",
    "OI_DELTA_5M",
    "OI_DELTA_1H",
    "OI_DELTA_24H",
    "FUNDING_Z",
    "RESILIENCY_SCORE",
    "RECOVERY_TIME_MS",
    "REFILL_VELOCITY",
    "KYLE_LAMBDA",
    "IMPACT_SCORE",
    "FRAGILITY_SCORE",
]
