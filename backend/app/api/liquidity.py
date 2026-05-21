"""Liquidity screener — top-N coins by market cap, restricted to symbols
that also trade on Binance USDT-M Futures.

CoinGecko is the universe source (free public API). Binance Futures
exchange info gives us the tradable set. We intersect the two so the LIQ
table only ever shows coins the rest of the screener can actually
analyze. Both upstream calls are cached briefly to avoid rate-limiting
on rapid page reloads — the data updates on the order of minutes, not
seconds, so a short TTL is fine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from kazus_db.models import LiquidityActiveSub, LiquiditySample
from kazus_logic.liquidity import REGISTRY

from ..db.base import get_db
from ..models.models import User
from .deps import get_current_user

logger = logging.getLogger("kazus.backend.liquidity")
router = APIRouter(prefix="/liquidity", tags=["liquidity"])

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
ALLOWED_LIMITS = (100, 250, 500)
PAGE_SIZE = 250  # CoinGecko per_page max
CACHE_TTL_S = 300  # 5 minutes


class LiqRow(BaseModel):
    rank: int
    coingecko_symbol: str         # e.g. "BTC", "PEPE"
    binance_symbol: str           # e.g. "BTCUSDT", "1000PEPEUSDT"
    name: str
    market_cap: Optional[float]
    volume_24h: Optional[float]
    price: Optional[float]
    change_24h_pct: Optional[float]
    image: Optional[str]


class LiqResponse(BaseModel):
    limit: int
    rows: List[LiqRow]
    fetched_at: float


# Module-level cache. Single-process worker, so a plain dict is enough —
# no need for redis here. Keyed by (source, limit_or_none).
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = asyncio.Lock()


async def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    async with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = await fn()
        _cache[key] = (now, value)
        return value


async def _fetch_binance_bases() -> dict[str, str]:
    """Return mapping `normalized_base -> binance_symbol`.

    Binance prefixes ultra-cheap tokens with "1000" (1000PEPEUSDT, etc.).
    The CoinGecko symbol for the same coin is just "PEPE", so we
    normalize both sides by stripping that prefix on the Binance side.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(BINANCE_FUTURES_EXCHANGE_INFO)
        r.raise_for_status()
        data = r.json()
    mapping: dict[str, str] = {}
    for s in data.get("symbols", []):
        if (
            s.get("contractType") != "PERPETUAL"
            or s.get("quoteAsset") != "USDT"
            or s.get("status") != "TRADING"
        ):
            continue
        symbol: str = s["symbol"]                  # "1000PEPEUSDT"
        base: str = s.get("baseAsset", "").upper()  # "1000PEPE"
        if base.startswith("1000"):
            base = base[4:]
        if not base:
            continue
        # First write wins — Binance shouldn't list duplicate bases anyway.
        mapping.setdefault(base, symbol)
    return mapping


async def _fetch_coingecko_top(limit: int) -> List[dict]:
    """Fetch the first `limit` coins by market cap from CoinGecko.

    `limit` is one of ALLOWED_LIMITS. CoinGecko's per_page maxes out at
    250, so 500 needs two pages.
    """
    pages = 1 if limit <= PAGE_SIZE else 2
    results: List[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for page in range(1, pages + 1):
            r = await client.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": PAGE_SIZE,
                    "page": page,
                    "sparkline": "false",
                },
            )
            r.raise_for_status()
            results.extend(r.json())
            if len(results) >= limit:
                break
    return results[:limit]


@router.get("/top", response_model=LiqResponse)
async def get_top(
    limit: int = Query(100),
    _user: User = Depends(get_current_user),
) -> LiqResponse:
    if limit not in ALLOWED_LIMITS:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be one of {ALLOWED_LIMITS}",
        )

    try:
        cg = await _cached(
            f"cg:{limit}", CACHE_TTL_S, lambda: _fetch_coingecko_top(limit)
        )
        binance_bases = await _cached(
            "binance:bases", CACHE_TTL_S, _fetch_binance_bases
        )
    except httpx.HTTPError as exc:
        logger.warning("liquidity upstream failed: %s", exc)
        raise HTTPException(status_code=502, detail="upstream fetch failed") from exc

    rows: List[LiqRow] = []
    for item in cg:
        sym = (item.get("symbol") or "").upper()
        if not sym:
            continue
        binance_symbol = binance_bases.get(sym)
        if not binance_symbol:
            continue
        rows.append(
            LiqRow(
                rank=int(item.get("market_cap_rank") or len(rows) + 1),
                coingecko_symbol=sym,
                binance_symbol=binance_symbol,
                name=item.get("name") or sym,
                market_cap=item.get("market_cap"),
                volume_24h=item.get("total_volume"),
                price=item.get("current_price"),
                change_24h_pct=item.get("price_change_percentage_24h"),
                image=item.get("image"),
            )
        )

    return LiqResponse(limit=limit, rows=rows, fetched_at=time.time())


# ── Metric time series ─────────────────────────────────────────────────────


class MetricMeta(BaseModel):
    name: str
    label: str


class MetricSampleOut(BaseModel):
    ts: int
    value: Optional[float]
    price: Optional[float]


class MetricSeriesResponse(BaseModel):
    symbol: str
    metric: str
    label: str
    window: str
    samples: List[MetricSampleOut]


_WINDOW_MS = {
    "1h": 3600 * 1000,
    "24h": 24 * 3600 * 1000,
    "7d": 7 * 24 * 3600 * 1000,
    "30d": 30 * 24 * 3600 * 1000,
}


@router.get("/metrics", response_model=List[MetricMeta])
async def list_metrics(_user: User = Depends(get_current_user)) -> List[MetricMeta]:
    """List every metric the worker is currently sampling — frontend uses
    this to decide which charts to render in the detail modal."""
    return [MetricMeta(name=m.name, label=m.label) for m in REGISTRY.values()]


@router.get("/metrics/{symbol}", response_model=MetricSeriesResponse)
async def get_metric_series(
    symbol: str,
    metric: str = Query(...),
    window: str = Query("24h"),
    since: Optional[int] = Query(None, description="If set, return only samples with ts > since (epoch ms)"),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MetricSeriesResponse:
    if metric not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    if window not in _WINDOW_MS:
        raise HTTPException(
            status_code=400,
            detail=f"window must be one of {sorted(_WINDOW_MS.keys())}",
        )
    symbol = symbol.upper()

    floor_ms = int(time.time() * 1000) - _WINDOW_MS[window]
    # `since` lets the frontend do incremental polling (live mode) — only
    # samples newer than the last seen ts are returned. When omitted, the
    # full window is returned for initial load.
    cutoff_ms = max(floor_ms, since) if since is not None else floor_ms
    rows = (
        db.query(LiquiditySample)
        .filter(
            LiquiditySample.symbol == symbol,
            LiquiditySample.metric == metric,
            LiquiditySample.ts > cutoff_ms,
        )
        .order_by(LiquiditySample.ts.asc())
        .all()
    )

    samples = [
        MetricSampleOut(ts=row.ts, value=row.value, price=row.price)
        for row in rows
    ]
    return MetricSeriesResponse(
        symbol=symbol,
        metric=metric,
        label=REGISTRY[metric].label,
        window=window,
        samples=samples,
    )


# ── Latest-per-metric snapshot (table columns) ─────────────────────────────


class MetricLatest(BaseModel):
    value: Optional[float]
    ts: int


class MetricsSnapshotResponse(BaseModel):
    symbols: dict[str, dict[str, MetricLatest]]


@router.get("/snapshot", response_model=MetricsSnapshotResponse)
async def get_metrics_snapshot(
    symbols: str = Query(..., description="Comma-separated Binance symbols"),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MetricsSnapshotResponse:
    """Latest sample per (symbol, metric) for a batch of symbols.

    Powers the LIQ scanner table — one row per symbol, one cell per
    metric, no history. Polled by the frontend every few seconds so
    metric columns reflect the worker's most recent write without
    re-fetching the entire CoinGecko universe.
    """
    parsed = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not parsed:
        return MetricsSnapshotResponse(symbols={})
    if len(parsed) > 500:
        raise HTTPException(status_code=400, detail="too many symbols (max 500)")

    from sqlalchemy import text
    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (symbol, metric)
                symbol, metric, value, ts
            FROM liquidity_samples
            WHERE symbol = ANY(:symbols)
            ORDER BY symbol, metric, ts DESC
            """
        ),
        {"symbols": parsed},
    ).fetchall()

    out: dict[str, dict[str, MetricLatest]] = {}
    for r in rows:
        out.setdefault(r.symbol, {})[r.metric] = MetricLatest(value=r.value, ts=r.ts)

    return MetricsSnapshotResponse(symbols=out)


# ── Realtime subscription heartbeat ────────────────────────────────────────


class ActiveSubIn(BaseModel):
    symbol: str
    ttl_seconds: int = 120  # bumped forward on each heartbeat


class ActiveSubOut(BaseModel):
    symbol: str
    expires_at: float


@router.post("/active", response_model=ActiveSubOut)
async def heartbeat_active_sub(
    payload: ActiveSubIn = Body(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ActiveSubOut:
    """Tell the worker to keep a WS subscription alive for `symbol`.

    UPSERT on `symbol`: first call inserts a row, subsequent calls (the
    frontend's 30s heartbeat) just bump `expires_at` forward. When the
    modal closes (or the user navigates away), heartbeats stop and the
    worker drops the subscription within ttl_seconds.
    """
    symbol = payload.symbol.upper()
    ttl = max(10, min(int(payload.ttl_seconds), 600))
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl)

    stmt = (
        pg_insert(LiquidityActiveSub)
        .values(symbol=symbol, expires_at=expires_at)
        .on_conflict_do_update(
            index_elements=["symbol"],
            set_={"expires_at": expires_at, "updated_at": datetime.now(timezone.utc).replace(tzinfo=None)},
        )
    )
    db.execute(stmt)
    db.commit()

    return ActiveSubOut(symbol=symbol, expires_at=expires_at.timestamp())
