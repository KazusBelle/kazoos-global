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
from typing import Iterable, List

import httpx
from sqlalchemy.orm import Session

from . import REGISTRY
from .base import MetricContext
from .binance_fetch import fetch_depth, fetch_h1_klines
from .universe import get_universe

logger = logging.getLogger("kazus.liquidity.poller")

POLL_INTERVAL_S = 60
TOP_N = 100
CONCURRENCY = 8       # parallel symbol fetches; 8×(klines+depth) ≈ 16 req/s burst
RETENTION_DAYS = 35   # keep ~1 month of samples


async def _process_symbol(
    client: httpx.AsyncClient,
    symbol: str,
    now_ts: int,
) -> List[dict]:
    """Fetch upstream for one symbol, run every metric in the registry,
    return rows ready for batch insert."""
    needs_klines = any("klines" in m.requires for m in REGISTRY.values())
    needs_depth = any("depth" in m.requires for m in REGISTRY.values())

    klines_task = fetch_h1_klines(client, symbol) if needs_klines else None
    depth_task = fetch_depth(client, symbol) if needs_depth else None

    klines, depth = await asyncio.gather(
        klines_task if klines_task else _none(),
        depth_task if depth_task else _none(),
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
    )

    rows: list[dict] = []
    for metric in REGISTRY.values():
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
        async def worker(sym: str):
            async with sem:
                rows = await _process_symbol(client, sym, now_ts)
                all_rows.extend(rows)

        await asyncio.gather(*(worker(s) for s in symbols), return_exceptions=True)

    if not all_rows:
        return 0

    # Batched insert — one round-trip per cycle for the whole TOP_N×metrics.
    from kazus_db.models import LiquiditySample

    with db_factory() as db:  # type: Session
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
            last_prune = now

        wait_s = max(1.0, POLL_INTERVAL_S - (time.monotonic() - t0))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
            break
        except asyncio.TimeoutError:
            pass
