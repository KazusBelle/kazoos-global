"""Liquidity polling loop.

Every POLL_INTERVAL_S, walk the top-N universe, fetch upstream data
once per symbol (klines + depth), dispatch to every metric in REGISTRY,
batch-insert the resulting samples into Postgres.

Symbols are processed with a bounded concurrency so we don't fan out
100 HTTP requests at once and trip Binance's per-IP rate limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Iterable, List, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import REGISTRY
from .base import MetricContext
from .binance_fetch import (
    fetch_depth,
    fetch_funding_all,
    fetch_h1_klines,
    fetch_open_interest,
)
from .metrics.derived import compute_derived
from .universe import get_universe

logger = logging.getLogger("kazus.liquidity.poller")

POLL_INTERVAL_S = 60
TOP_N = 100
CONCURRENCY = 8       # parallel symbol fetches; 8×(klines+depth) ≈ 16 req/s burst
RETENTION_DAYS = 35   # keep ~1 month of samples


def _rest_metrics_real():
    """REST metrics that participate in the 60s per-symbol dispatch.

    WS metrics (source=ws) are owned by the realtime sampler. Derived
    metrics (source=derived — oi_delta_*, funding_z) need history and
    are computed in a separate post-pass, not per-symbol context.
    """
    return [
        m for m in REGISTRY.values()
        if getattr(m, "source", "rest") == "rest"
    ]


async def _process_symbol(
    client: httpx.AsyncClient,
    symbol: str,
    now_ts: int,
    funding_rate: Optional[float],
) -> List[dict]:
    """Fetch upstream for one symbol, run every REST metric in the
    registry, return rows ready for batch insert. `funding_rate` is
    pre-fetched in a single bulk call upstream — we just pass it in."""
    rest_metrics = _rest_metrics_real()
    needs_klines = any("klines" in m.requires for m in rest_metrics)
    needs_depth = any("depth" in m.requires for m in rest_metrics)
    needs_oi = any("open_interest" in m.requires for m in rest_metrics)

    klines_task = fetch_h1_klines(client, symbol) if needs_klines else None
    depth_task = fetch_depth(client, symbol) if needs_depth else None
    oi_task = fetch_open_interest(client, symbol) if needs_oi else None

    klines, depth, open_interest = await asyncio.gather(
        klines_task if klines_task else _none(),
        depth_task if depth_task else _none(),
        oi_task if oi_task else _none(),
    )

    price = None
    if klines:
        price = klines[-1].close
    elif depth and depth.bids and depth.asks:
        price = (depth.bids[0][0] + depth.asks[0][0]) / 2

    ctx = MetricContext(
        symbol=symbol,
        now_ts=now_ts,
        price=price,
        h1_klines=klines,
        depth=depth,
        open_interest=open_interest,
        funding_rate=funding_rate,
    )

    rows: list[dict] = []
    for metric in rest_metrics:
        try:
            value = await metric.compute(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("metric %s failed for %s: %s", metric.name, symbol, exc)
            value = None
        rows.append({
            "symbol": symbol,
            "metric": metric.name,
            "ts": now_ts,
            "value": value,
            "price": price,
        })
    return rows


async def _none():
    return None


async def run_cycle(db_factory) -> int:
    """One polling pass over the TOP_N universe. Returns rows written."""
    universe = await get_universe(TOP_N)
    if not universe:
        logger.warning("liquidity universe empty — skipping cycle")
        return 0

    symbols = [u.binance_symbol for u in universe]
    now_ts = int(time.time() * 1000)
    sem = asyncio.Semaphore(CONCURRENCY)
    all_rows: list[dict] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        # One call gives every symbol's current funding rate, vs N
        # per-symbol calls. Done upfront so each worker just looks up.
        funding_map = await fetch_funding_all(client) or {}

        async def worker(sym: str):
            async with sem:
                rows = await _process_symbol(client, sym, now_ts, funding_map.get(sym))
                all_rows.extend(rows)

        await asyncio.gather(*(worker(s) for s in symbols), return_exceptions=True)

    if not all_rows:
        return 0

    # Derived metrics (oi deltas, funding z-score) need history — one
    # bulk SQL per metric family is much cheaper than per-symbol queries.
    current_by_symbol: dict[str, dict[str, Optional[float]]] = {}
    for r in all_rows:
        sym = r["symbol"]
        cur = current_by_symbol.setdefault(sym, {"oi": None, "funding": None, "price": None})
        if r["metric"] == "oi":
            cur["oi"] = r["value"]
        elif r["metric"] == "funding":
            cur["funding"] = r["value"]
        if cur["price"] is None:
            cur["price"] = r.get("price")

    # Batched insert — one round-trip per cycle for the whole TOP_N×metrics.
    from kazus_db.models import LiquiditySample

    with db_factory() as db:  # type: Session
        try:
            derived_rows = compute_derived(db, current_by_symbol, now_ts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("derived metrics failed: %s", exc)
            derived_rows = []
        if derived_rows:
            all_rows.extend(derived_rows)
        db.bulk_insert_mappings(LiquiditySample, all_rows)
        db.commit()

    return len(all_rows)


async def prune_old(db_factory, retention_days: int = RETENTION_DAYS) -> int:
    """Drop samples older than retention_days. Runs alongside the
    polling loop on a separate, slower cadence."""
    from kazus_db.models import LiquiditySample

    cutoff_ms = int((time.time() - retention_days * 86400) * 1000)
    with db_factory() as db:  # type: Session
        deleted = (
            db.query(LiquiditySample)
            .filter(LiquiditySample.ts < cutoff_ms)
            .delete(synchronize_session=False)
        )
        db.commit()
    return int(deleted or 0)


# Retention horizons for the slow-growth research tables. Picked from the
# 2026-05-23 audit:
#   - alert_history: ~700/day → ~250K/y, 90d ≈ 63K rows (small but
#     bounded; aggregations need a recent window anyway)
#   - intelligence_history: 5-min cadence → 288/day max → 90d ≈ 26K rows
#   - anomaly_memory + anomaly_edges: ~10/day → 180d ≈ 1.8K rows; the
#     genealogy graph is more valuable than samples but still needs a cap
#   - crossex_history: ~10/day → 90d ≈ 900 rows
ALERT_HISTORY_RETENTION_DAYS = 90
INTELLIGENCE_HISTORY_RETENTION_DAYS = 90
ANOMALY_RETENTION_DAYS = 180
CROSSEX_RETENTION_DAYS = 90


async def prune_research_tables(db_factory) -> Dict[str, int]:
    """Prune the slow-growth research tables. Returns per-table deletion
    counts so the worker can log a single line. Independent of the
    sample-pruning function so callers can tune cadence separately."""
    from kazus_db.models import (
        LiquidityAlertHistory,
        LiquidityIntelligenceHistory,
        LiquidityAnomalyMemory,
        LiquidityCrossExHistory,
    )

    now_ms = int(time.time() * 1000)
    H = 86_400_000
    cutoffs = {
        "alert_history":        (LiquidityAlertHistory,        "started_at_ms",  now_ms - ALERT_HISTORY_RETENTION_DAYS * H),
        "intelligence_history": (LiquidityIntelligenceHistory, "ts_ms",          now_ms - INTELLIGENCE_HISTORY_RETENTION_DAYS * H),
        "anomaly_memory":       (LiquidityAnomalyMemory,       "occurred_at_ms", now_ms - ANOMALY_RETENTION_DAYS * H),
        "crossex_history":      (LiquidityCrossExHistory,      "ts_ms",          now_ms - CROSSEX_RETENTION_DAYS * H),
    }
    deleted: Dict[str, int] = {}
    with db_factory() as db:  # type: Session
        for label, (model, ts_col, cutoff) in cutoffs.items():
            try:
                n = (
                    db.query(model)
                    .filter(getattr(model, ts_col) < cutoff)
                    .delete(synchronize_session=False)
                )
                deleted[label] = int(n or 0)
            except Exception as exc:  # noqa: BLE001
                logger.exception("prune %s failed: %s", label, exc)
                deleted[label] = -1
        # anomaly_edges: prune edges pointing to records that no longer
        # exist (FK-equivalent cleanup, since we delete anomaly_memory
        # above without ON DELETE CASCADE).
        try:
            n = db.execute(text(
                "DELETE FROM liquidity_anomaly_edges "
                "WHERE from_id NOT IN (SELECT id FROM liquidity_anomaly_memory) "
                "OR to_id NOT IN (SELECT id FROM liquidity_anomaly_memory)"
            )).rowcount
            deleted["anomaly_edges"] = int(n or 0)
        except Exception as exc:  # noqa: BLE001
            logger.exception("prune anomaly_edges failed: %s", exc)
            deleted["anomaly_edges"] = -1
        db.commit()
    return deleted


async def loop(db_factory, stop_event: asyncio.Event) -> None:
    """Top-level loop. Polls every POLL_INTERVAL_S, prunes once an hour."""
    last_prune = 0.0
    while not stop_event.is_set():
        t0 = time.monotonic()
        try:
            n = await run_cycle(db_factory)
            elapsed = time.monotonic() - t0
            logger.info(
                "liquidity cycle: %d rows in %.1fs", n, elapsed
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("liquidity cycle failed: %s", exc)

        now = time.monotonic()
        if now - last_prune > 3600:
            try:
                pruned = await prune_old(db_factory)
                if pruned:
                    logger.info("liquidity prune: dropped %d old samples", pruned)
            except Exception as exc:  # noqa: BLE001
                logger.exception("liquidity prune failed: %s", exc)
            try:
                res_pruned = await prune_research_tables(db_factory)
                non_zero = {k: v for k, v in res_pruned.items() if v}
                if non_zero:
                    logger.info("research prune: %s", non_zero)
            except Exception as exc:  # noqa: BLE001
                logger.exception("research prune failed: %s", exc)
            last_prune = now

        wait_s = max(1.0, POLL_INTERVAL_S - (time.monotonic() - t0))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
            break
        except asyncio.TimeoutError:
            pass
