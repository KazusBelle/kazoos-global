"""Derived metrics: OI deltas + funding z-score.

These don't fit the per-symbol MetricContext flow because they need
history from `liquidity_samples`. The poller calls `compute_derived`
once per cycle (one bulk SQL per metric family) after the main metric
dispatch, then appends the results to the same bulk-insert batch.

Each entry registered here exposes only the `name`/`label`/`source`
fields the API needs to surface it in the metric list. The actual
compute happens in `compute_derived` below, not in `compute()`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..base import MetricContext


_5M = 5 * 60 * 1000
_1H = 60 * 60 * 1000
_24H = 24 * 60 * 60 * 1000
_7D = 7 * _24H

# Tolerance window for picking "the sample closest to T ago" — too tight
# and we miss when a poll cycle drifts, too loose and we compare against
# a stale value. ±90s for 5m, ±15m for 1h, ±2h for 24h.
_TOL_5M = 90 * 1000
_TOL_1H = 15 * 60 * 1000
_TOL_24H = 2 * 3600 * 1000


@dataclass
class _Stub:
    """Placeholder so the metric appears in REGISTRY without breaking the
    polling loop's protocol. `compute()` is never called for derived
    metrics — the poller routes them through `compute_derived`."""
    name: str
    label: str
    requires: tuple = ()
    source: str = "derived"

    async def compute(self, _ctx: MetricContext) -> Optional[float]:
        return None


OI_DELTA_5M = _Stub(name="oi_delta_5m", label="OI Δ 5m (%)")
OI_DELTA_1H = _Stub(name="oi_delta_1h", label="OI Δ 1h (%)")
OI_DELTA_24H = _Stub(name="oi_delta_24h", label="OI Δ 24h (%)")
FUNDING_Z = _Stub(name="funding_z", label="Funding Z (7d)")

DERIVED = (OI_DELTA_5M, OI_DELTA_1H, OI_DELTA_24H, FUNDING_Z)


def _pick_closest(series: List[tuple[int, float]], target_ts: int, tol_ms: int) -> Optional[float]:
    """series is ascending by ts. Pick the sample whose ts is closest to
    target_ts AND within tol_ms — otherwise None (don't fabricate a delta
    from a sample that's wildly off-target)."""
    if not series:
        return None
    best: Optional[tuple[int, float]] = None
    best_dt = None
    for ts, v in series:
        dt = abs(ts - target_ts)
        if best_dt is None or dt < best_dt:
            best = (ts, v)
            best_dt = dt
    if best is None or best_dt is None or best_dt > tol_ms:
        return None
    return best[1]


def _pct_delta(current: float, past: Optional[float]) -> Optional[float]:
    if past is None or past == 0:
        return None
    return (current - past) / past * 100.0


def compute_derived(
    db,
    current_by_symbol: Dict[str, Dict[str, Optional[float]]],
    now_ts: int,
) -> List[dict]:
    """Compute oi_delta_* and funding_z rows from history.

    current_by_symbol: {symbol: {"oi": <usd>, "funding": <rate>, "price": <px>}}
    Returns list of sample dicts ready for bulk_insert_mappings.
    """
    from kazus_db.models import LiquiditySample
    from sqlalchemy import and_

    symbols = list(current_by_symbol.keys())
    if not symbols:
        return []

    # Pull OI history (24h+) and funding history (7d) in two bulk queries.
    oi_rows = (
        db.query(LiquiditySample.symbol, LiquiditySample.ts, LiquiditySample.value)
        .filter(
            and_(
                LiquiditySample.symbol.in_(symbols),
                LiquiditySample.metric == "oi",
                LiquiditySample.ts >= now_ts - _24H - _TOL_24H,
            )
        )
        .order_by(LiquiditySample.symbol.asc(), LiquiditySample.ts.asc())
        .all()
    )
    funding_rows = (
        db.query(LiquiditySample.symbol, LiquiditySample.ts, LiquiditySample.value)
        .filter(
            and_(
                LiquiditySample.symbol.in_(symbols),
                LiquiditySample.metric == "funding",
                LiquiditySample.ts >= now_ts - _7D,
            )
        )
        .order_by(LiquiditySample.symbol.asc(), LiquiditySample.ts.asc())
        .all()
    )

    oi_hist: Dict[str, List[tuple[int, float]]] = {}
    for sym, ts, val in oi_rows:
        if val is None:
            continue
        oi_hist.setdefault(sym, []).append((int(ts), float(val)))

    funding_hist: Dict[str, List[float]] = {}
    for sym, _ts, val in funding_rows:
        if val is None:
            continue
        funding_hist.setdefault(sym, []).append(float(val))

    out: List[dict] = []
    for sym, cur in current_by_symbol.items():
        price = cur.get("price")
        cur_oi = cur.get("oi")
        cur_funding = cur.get("funding")

        # OI deltas
        if cur_oi is not None:
            series = oi_hist.get(sym, [])
            for metric_name, dt_ms, tol in (
                ("oi_delta_5m", _5M, _TOL_5M),
                ("oi_delta_1h", _1H, _TOL_1H),
                ("oi_delta_24h", _24H, _TOL_24H),
            ):
                past = _pick_closest(series, now_ts - dt_ms, tol)
                delta_pct = _pct_delta(cur_oi, past)
                out.append({
                    "symbol": sym,
                    "metric": metric_name,
                    "ts": now_ts,
                    "value": delta_pct,
                    "price": price,
                })
        else:
            for metric_name in ("oi_delta_5m", "oi_delta_1h", "oi_delta_24h"):
                out.append({
                    "symbol": sym, "metric": metric_name,
                    "ts": now_ts, "value": None, "price": price,
                })

        # Funding z-score over 7d history. Need ≥10 samples to be meaningful.
        z: Optional[float] = None
        if cur_funding is not None:
            hist = funding_hist.get(sym, [])
            if len(hist) >= 10:
                mean = sum(hist) / len(hist)
                var = sum((x - mean) ** 2 for x in hist) / len(hist)
                std = math.sqrt(var)
                if std > 1e-9:
                    z = (cur_funding - mean) / std
        out.append({
            "symbol": sym, "metric": "funding_z",
            "ts": now_ts, "value": z, "price": price,
        })

    return out
