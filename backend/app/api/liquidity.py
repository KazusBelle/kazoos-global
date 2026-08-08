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
from typing import Any, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import case, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import json as _json

from kazus_db.models import (
    LiquidityActiveSub,
    LiquidityAlertHistory,
    LiquidityAnnotation,
    LiquidityCrossExHistory,
    LiquidityPin,
    LiquiditySample,
    LiquidityWsStatus,
)
from kazus_logic.liquidity import REGISTRY

PIN_CAP = 20

from ..core.config import get_settings
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


# Module-level cache. Single-process backend on one event loop, so a plain
# dict + per-key asyncio locks are enough — no redis. Keyed by (source, limit).
# Entry shape: key -> (updated_at, value).
_cache: dict[str, tuple[float, object]] = {}
# Per-key locks (NOT one global lock): a slow CoinGecko refresh must never
# serialize the unrelated Binance key (or vice-versa).
_key_locks: dict[str, asyncio.Lock] = {}
# Keys with an in-flight background refresh — dedupes concurrent refreshes so
# only ONE runs per key (bounded; never unbounded task creation).
_refreshing: set[str] = set()
# Strong refs to background refresh tasks so they aren't GC'd mid-flight.
_bg_refresh_tasks: set = set()


def _key_lock(key: str) -> asyncio.Lock:
    lk = _key_locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _key_locks[key] = lk
    return lk


async def _refresh_cache_key(key: str, fn) -> None:
    """Background refresh for a stale key. Runs as an independent task so it
    survives the originating request's cancellation. On failure it KEEPS the
    last-good value (never deletes) and logs a warning."""
    try:
        async with _key_lock(key):
            value = await fn()
            _cache[key] = (time.time(), value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache refresh failed for %s: %s (keeping stale value)", key, exc)
    finally:
        _refreshing.discard(key)


def _schedule_refresh(key: str, fn) -> None:
    # Dedupe: at most one background refresh per key (no unbounded tasks).
    # No await between the membership check and add() → race-free on the loop.
    if key in _refreshing:
        return
    _refreshing.add(key)
    task = asyncio.create_task(_refresh_cache_key(key, fn))
    _bg_refresh_tasks.add(task)
    task.add_done_callback(_bg_refresh_tasks.discard)


async def _cached(key: str, ttl: float, fn):
    """Stale-while-revalidate cache.

    - Fresh entry (age < ttl): return it.
    - Stale entry: return the stale value IMMEDIATELY and trigger a background
      refresh (the user request never blocks on the slow external fetch, and
      the refresh survives request cancellation).
    - No entry at all: fetch synchronously once under the PER-KEY lock (so the
      first-ever load still works), double-checking after acquiring the lock.
    """
    now = time.time()
    hit = _cache.get(key)
    if hit is not None:
        updated_at, value = hit
        if now - updated_at < ttl:
            return value  # fresh
        # stale → serve last-good now, refresh in background (non-blocking)
        _schedule_refresh(key, fn)
        return value
    # Cold (no last-good value): must fetch once. Per-key lock prevents a
    # stampede on this key without blocking other keys.
    async with _key_lock(key):
        hit = _cache.get(key)
        if hit is not None:
            return hit[1]
        value = await fn()
        _cache[key] = (time.time(), value)
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


# Process-wide cache of the distinct metric-name list in liquidity_samples.
# New metric names are added very rarely (a new feature deployment, weeks
# apart), so a 10-minute TTL is generous. The snapshot query uses this
# list to do one indexed LIMIT-1 lookup per (symbol, metric) instead of
# the old DISTINCT ON pattern that read all historical rows per symbol.
_metric_names_cache: Optional[tuple[float, tuple[str, ...]]] = None
_METRIC_NAMES_TTL_S = 600


def _get_known_metric_names(db: Session) -> tuple[str, ...]:
    global _metric_names_cache
    now = time.time()
    if _metric_names_cache and now - _metric_names_cache[0] < _METRIC_NAMES_TTL_S:
        return _metric_names_cache[1]
    from sqlalchemy import text as _text
    rows = db.execute(_text("SELECT DISTINCT metric FROM liquidity_samples")).fetchall()
    names = tuple(sorted(r[0] for r in rows))
    _metric_names_cache = (now, names)
    return names


# Shared LATERAL-join query body. PostgreSQL doesn't support a true
# loose-index-scan, so the old `DISTINCT ON (symbol, metric) ORDER BY ts
# DESC` plan read every historical row for the requested symbols (1.7M
# rows scanned for ~60 symbols, ~5s execution). Cross-joining symbols ×
# metric-names and doing one indexed `LIMIT 1` per pair against
# ix_liq_samples_symbol_metric_ts collapses that to ~2200 tight index
# lookups (~20ms in EXPLAIN ANALYZE, 240× speedup). Result rows and
# response shape are identical to the prior query.
_SNAPSHOT_SQL_LIVE = """
SELECT s.sym AS symbol, m.met AS metric, l.value, l.ts
FROM unnest(CAST(:symbols AS text[])) AS s(sym)
CROSS JOIN unnest(CAST(:metrics AS text[])) AS m(met)
INNER JOIN LATERAL (
    SELECT value, ts FROM liquidity_samples
    WHERE symbol = s.sym AND metric = m.met
    ORDER BY ts DESC
    LIMIT 1
) l ON true
"""

_SNAPSHOT_SQL_REPLAY = """
SELECT s.sym AS symbol, m.met AS metric, l.value, l.ts
FROM unnest(CAST(:symbols AS text[])) AS s(sym)
CROSS JOIN unnest(CAST(:metrics AS text[])) AS m(met)
INNER JOIN LATERAL (
    SELECT value, ts FROM liquidity_samples
    WHERE symbol = s.sym AND metric = m.met AND ts <= :as_of
    ORDER BY ts DESC
    LIMIT 1
) l ON true
"""


@router.get("/snapshot/replay", response_model=MetricsSnapshotResponse)
async def get_snapshot_replay(
    symbols: str = Query(..., description="Comma-separated Binance symbols"),
    as_of: int = Query(..., description="Epoch-ms timestamp to reconstruct snapshot at"),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MetricsSnapshotResponse:
    """Latest sample per (symbol, metric) where ts ≤ as_of.

    The replay UI scrubs through time and re-renders the scanner using
    historical samples; this is the as-of-time variant of /snapshot.
    Restricted to the same 500-symbol batch limit as the live endpoint
    so we don't accidentally point it at the entire history.
    """
    parsed = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not parsed:
        return MetricsSnapshotResponse(symbols={})
    if len(parsed) > 500:
        raise HTTPException(status_code=400, detail="too many symbols (max 500)")
    if as_of <= 0:
        raise HTTPException(status_code=400, detail="as_of must be > 0")

    metric_names = _get_known_metric_names(db)
    if not metric_names:
        return MetricsSnapshotResponse(symbols={})

    from sqlalchemy import text
    rows = db.execute(
        text(_SNAPSHOT_SQL_REPLAY),
        {"symbols": parsed, "metrics": list(metric_names), "as_of": as_of},
    ).fetchall()

    out: dict[str, dict[str, MetricLatest]] = {}
    for r in rows:
        out.setdefault(r.symbol, {})[r.metric] = MetricLatest(value=r.value, ts=r.ts)
    return MetricsSnapshotResponse(symbols=out)


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

    metric_names = _get_known_metric_names(db)
    if not metric_names:
        return MetricsSnapshotResponse(symbols={})

    from sqlalchemy import text
    rows = db.execute(
        text(_SNAPSHOT_SQL_LIVE),
        {"symbols": parsed, "metrics": list(metric_names)},
    ).fetchall()

    out: dict[str, dict[str, MetricLatest]] = {}
    for r in rows:
        out.setdefault(r.symbol, {})[r.metric] = MetricLatest(value=r.value, ts=r.ts)

    return MetricsSnapshotResponse(symbols=out)


# ── Replay range ───────────────────────────────────────────────────────────


class ReplayRangeOut(BaseModel):
    earliest_ts: Optional[int]
    latest_ts: Optional[int]


@router.get("/replay/range", response_model=ReplayRangeOut)
async def get_replay_range(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ReplayRangeOut:
    """min/max ts across the entire liquidity_samples table.

    Bounds the replay slider so the UI doesn't let you scrub to a window
    with no data. Cheap query — both ends are an indexed MIN/MAX.
    """
    from sqlalchemy import func
    row = db.execute(
        select(
            func.min(LiquiditySample.ts).label("earliest"),
            func.max(LiquiditySample.ts).label("latest"),
        )
    ).first()
    earliest = int(row.earliest) if row and row.earliest is not None else None
    latest = int(row.latest) if row and row.latest is not None else None
    return ReplayRangeOut(earliest_ts=earliest, latest_ts=latest)


# ── Cross-exchange validation ──────────────────────────────────────────────


class CrossExSnapshotOut(BaseModel):
    exchange: str
    symbol: str
    funding_rate: Optional[float]
    open_interest_usd: Optional[float]
    spread_fraction: Optional[float]
    mid_price: Optional[float]
    ts_ms: int


class CrossExDivergence(BaseModel):
    """Pairwise diff vs the reference exchange (Binance).

    Each field is `(this − reference) / reference` so the UI can render
    a +/-% gauge. Mid-price divergence is the canonical anti-manipulation
    signal — sustained price separation across major venues is rare and
    almost always means something is wrong on one side.
    """
    exchange: str
    funding_diff: Optional[float]      # absolute diff in fraction
    oi_diff_pct: Optional[float]
    spread_diff_pct: Optional[float]
    mid_price_diff_pct: Optional[float]


class CrossExResponse(BaseModel):
    symbol: str
    snapshots: List[CrossExSnapshotOut]
    divergences: List[CrossExDivergence]
    reference: str = "binance"
    fetched_at_ms: int


@router.get("/crossex/{symbol}", response_model=CrossExResponse)
async def get_crossex(
    symbol: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CrossExResponse:
    """Compare a symbol across registered exchanges (currently Binance +
    Bybit). Fired ad-hoc by the detail modal — not cached server-side
    because the data is already cheap and the UI debounces on its end.
    """
    from kazus_logic.liquidity.exchanges import REGISTRY as EX_REGISTRY

    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(
            *(ex.fetch_snapshot(client, sym) for ex in EX_REGISTRY.values()),
            return_exceptions=True,
        )

    snapshots: List[CrossExSnapshotOut] = []
    reference = None
    for r in results:
        if isinstance(r, Exception) or r is None:
            continue
        snap = CrossExSnapshotOut(
            exchange=r.exchange,
            symbol=r.symbol,
            funding_rate=r.funding_rate,
            open_interest_usd=r.open_interest_usd,
            spread_fraction=r.spread_fraction,
            mid_price=r.mid_price,
            ts_ms=r.ts_ms,
        )
        snapshots.append(snap)
        if snap.exchange == "binance":
            reference = snap

    divergences: List[CrossExDivergence] = []
    if reference is not None:
        for snap in snapshots:
            if snap.exchange == reference.exchange:
                continue
            divergences.append(
                CrossExDivergence(
                    exchange=snap.exchange,
                    funding_diff=_safe_diff(snap.funding_rate, reference.funding_rate),
                    oi_diff_pct=_safe_pct_diff(snap.open_interest_usd, reference.open_interest_usd),
                    spread_diff_pct=_safe_pct_diff(snap.spread_fraction, reference.spread_fraction),
                    mid_price_diff_pct=_safe_pct_diff(snap.mid_price, reference.mid_price),
                )
            )

    # Persist every responding venue so /research/venue-quality has data
    # to aggregate over time. We only record real snapshots — exceptions
    # / Nones were filtered out above.
    try:
        for snap in snapshots:
            db.add(LiquidityCrossExHistory(
                symbol=snap.symbol,
                exchange=snap.exchange,
                ts_ms=snap.ts_ms,
                funding_rate=snap.funding_rate,
                open_interest_usd=snap.open_interest_usd,
                spread_fraction=snap.spread_fraction,
                mid_price=snap.mid_price,
            ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("crossex history insert failed: %s", exc)
        db.rollback()

    return CrossExResponse(
        symbol=sym,
        snapshots=snapshots,
        divergences=divergences,
        fetched_at_ms=int(time.time() * 1000),
    )


def _safe_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return a - b


def _safe_pct_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100


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


# ── Pinned symbols ─────────────────────────────────────────────────────────


class LiquidityPinOut(BaseModel):
    symbol: str
    pinned_order: int


def _normalize_pin_orders(db: Session) -> None:
    """Rewrite pinned_order values to be contiguous 0..N-1, preserving order."""
    pins = (
        db.query(LiquidityPin)
        .order_by(LiquidityPin.pinned_order.asc())
        .all()
    )
    for idx, p in enumerate(pins):
        p.pinned_order = idx


@router.get("/pins", response_model=List[LiquidityPinOut])
async def list_pins(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> List[LiquidityPinOut]:
    rows = (
        db.query(LiquidityPin)
        .order_by(LiquidityPin.pinned_order.asc())
        .all()
    )
    return [LiquidityPinOut(symbol=r.symbol, pinned_order=r.pinned_order) for r in rows]


class PinIn(BaseModel):
    symbol: str


@router.post("/pins", response_model=LiquidityPinOut)
async def add_pin(
    payload: PinIn = Body(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> LiquidityPinOut:
    """Pin a symbol. Worker will begin streaming it within ~5s.

    The active (modal-open) symbol gets an extra slot beyond PIN_CAP, but
    explicit pins are hard-capped to keep the WS stream-set bounded.
    """
    symbol = payload.symbol.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")

    existing = db.query(LiquidityPin).filter(LiquidityPin.symbol == symbol).first()
    if existing is not None:
        return LiquidityPinOut(symbol=existing.symbol, pinned_order=existing.pinned_order)

    if db.query(LiquidityPin).count() >= PIN_CAP:
        raise HTTPException(
            status_code=409,
            detail=f"pin cap reached ({PIN_CAP}) — unpin something first",
        )

    max_order = db.query(LiquidityPin.pinned_order).order_by(LiquidityPin.pinned_order.desc()).first()
    next_order = 0 if max_order is None else int(max_order[0]) + 1
    row = LiquidityPin(symbol=symbol, pinned_order=next_order)
    db.add(row)
    db.commit()
    db.refresh(row)
    return LiquidityPinOut(symbol=row.symbol, pinned_order=row.pinned_order)


@router.delete("/pins/{symbol}", status_code=204, response_class=Response)
async def remove_pin(
    symbol: str,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    sym = symbol.strip().upper()
    row = db.query(LiquidityPin).filter(LiquidityPin.symbol == sym).first()
    if row is None:
        return Response(status_code=204)
    db.delete(row)
    db.flush()
    _normalize_pin_orders(db)
    db.commit()
    return Response(status_code=204)


class MovePinIn(BaseModel):
    direction: str  # "up" | "down"


@router.post("/pins/{symbol}/move", response_model=List[LiquidityPinOut])
async def move_pin(
    symbol: str,
    payload: MovePinIn = Body(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> List[LiquidityPinOut]:
    sym = symbol.strip().upper()
    if payload.direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction must be 'up' or 'down'")
    row = db.query(LiquidityPin).filter(LiquidityPin.symbol == sym).first()
    if row is None:
        raise HTTPException(status_code=404, detail="symbol is not pinned")

    q = db.query(LiquidityPin)
    if payload.direction == "up":
        neighbor = (
            q.filter(LiquidityPin.pinned_order < row.pinned_order)
            .order_by(LiquidityPin.pinned_order.desc())
            .first()
        )
    else:
        neighbor = (
            q.filter(LiquidityPin.pinned_order > row.pinned_order)
            .order_by(LiquidityPin.pinned_order.asc())
            .first()
        )
    if neighbor is None:
        # Already at the edge — no-op, return current order
        all_rows = q.order_by(LiquidityPin.pinned_order.asc()).all()
        return [LiquidityPinOut(symbol=r.symbol, pinned_order=r.pinned_order) for r in all_rows]
    row.pinned_order, neighbor.pinned_order = neighbor.pinned_order, row.pinned_order
    db.commit()
    all_rows = q.order_by(LiquidityPin.pinned_order.asc()).all()
    return [LiquidityPinOut(symbol=r.symbol, pinned_order=r.pinned_order) for r in all_rows]


# ── WS health ──────────────────────────────────────────────────────────────


class WsStatusOut(BaseModel):
    conn_id: int
    connected: bool
    subscribed: List[str]
    last_message_at: Optional[float]  # epoch seconds
    updated_at: Optional[float]


@router.get("/ws/status", response_model=WsStatusOut)
async def ws_status(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> WsStatusOut:
    row = db.query(LiquidityWsStatus).filter(LiquidityWsStatus.id == 1).first()
    if row is None:
        return WsStatusOut(conn_id=0, connected=False, subscribed=[], last_message_at=None, updated_at=None)
    try:
        subscribed = _json.loads(row.subscribed_json) if row.subscribed_json else []
    except (TypeError, ValueError):
        subscribed = []
    last_msg = row.last_message_at.replace(tzinfo=timezone.utc).timestamp() if row.last_message_at else None
    updated = row.updated_at.replace(tzinfo=timezone.utc).timestamp() if row.updated_at else None
    return WsStatusOut(
        conn_id=row.conn_id,
        connected=row.connected,
        subscribed=list(subscribed),
        last_message_at=last_msg,
        updated_at=updated,
    )


# ══════════════════════════════════════════════════════════════════════════
#  Phase-7 research layer
# ══════════════════════════════════════════════════════════════════════════
#
# Persists alerts emitted by the client-side engine and exposes aggregate
# queries the Research page consumes: signal-stats per kind, drift series,
# similarity matching, venue quality, annotation CRUD.
#
# Why not just compute everything client-side? Because alerts originate
# in the browser and disappear on reload — we need server-side storage
# for any cross-session statistics. Once persisted, the heavy aggregation
# is a couple of GROUP BYs over indexed columns: cheap, no worker job.

# ── Alert history ─────────────────────────────────────────────────────────


class AlertLogIn(BaseModel):
    alert_id: str
    symbol: str
    kind: str
    severity: str
    regime: str
    confidence: float
    priority: float
    trigger: str = ""
    started_at_ms: int
    last_seen_at_ms: int


class AlertLogOut(BaseModel):
    alert_id: str
    symbol: str
    kind: str
    severity: str
    regime: str
    confidence: float
    priority: float
    trigger: str
    started_at_ms: int
    last_seen_at_ms: int
    validated_outcome: Optional[str]
    validated_at_ms: Optional[int]
    validation_notes: Optional[str]


@router.post("/alerts", response_model=AlertLogOut)
async def log_alert(
    payload: AlertLogIn = Body(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AlertLogOut:
    """UPSERT on alert_id. The client may re-post the same alert as
    severity escalates over the cooldown window — we just refresh
    last_seen_at + severity + priority + confidence."""
    stmt = (
        pg_insert(LiquidityAlertHistory)
        .values(
            alert_id=payload.alert_id,
            symbol=payload.symbol.upper(),
            kind=payload.kind,
            severity=payload.severity,
            regime=payload.regime,
            confidence=payload.confidence,
            priority=payload.priority,
            trigger=payload.trigger,
            started_at_ms=payload.started_at_ms,
            last_seen_at_ms=payload.last_seen_at_ms,
        )
        .on_conflict_do_update(
            index_elements=["alert_id"],
            set_={
                "severity": payload.severity,
                "regime": payload.regime,
                "confidence": payload.confidence,
                "priority": payload.priority,
                "last_seen_at_ms": payload.last_seen_at_ms,
                "trigger": payload.trigger,
            },
        )
    )
    db.execute(stmt)
    db.commit()
    row = (
        db.query(LiquidityAlertHistory)
        .filter(LiquidityAlertHistory.alert_id == payload.alert_id)
        .first()
    )
    return AlertLogOut(
        alert_id=row.alert_id, symbol=row.symbol, kind=row.kind,
        severity=row.severity, regime=row.regime, confidence=row.confidence,
        priority=row.priority, trigger=row.trigger,
        started_at_ms=row.started_at_ms, last_seen_at_ms=row.last_seen_at_ms,
        validated_outcome=row.validated_outcome,
        validated_at_ms=row.validated_at_ms,
        validation_notes=row.validation_notes,
    )


class AlertValidatePatch(BaseModel):
    validated_outcome: str   # "followed_through" | "noise" | "pending"
    notes: Optional[str] = None


@router.patch("/alerts/{alert_id}/validate", response_model=AlertLogOut)
async def validate_alert(
    alert_id: str,
    payload: AlertValidatePatch = Body(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AlertLogOut:
    row = (
        db.query(LiquidityAlertHistory)
        .filter(LiquidityAlertHistory.alert_id == alert_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    if payload.validated_outcome not in ("followed_through", "noise", "pending"):
        raise HTTPException(status_code=400, detail="invalid outcome")
    row.validated_outcome = payload.validated_outcome
    row.validated_at_ms = int(time.time() * 1000)
    row.validation_notes = payload.notes
    db.commit()
    db.refresh(row)
    return AlertLogOut(
        alert_id=row.alert_id, symbol=row.symbol, kind=row.kind,
        severity=row.severity, regime=row.regime, confidence=row.confidence,
        priority=row.priority, trigger=row.trigger,
        started_at_ms=row.started_at_ms, last_seen_at_ms=row.last_seen_at_ms,
        validated_outcome=row.validated_outcome,
        validated_at_ms=row.validated_at_ms,
        validation_notes=row.validation_notes,
    )


@router.get("/alerts", response_model=List[AlertLogOut])
async def list_alerts(
    symbol: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    since_ms: Optional[int] = Query(None),
    limit: int = Query(200, le=2000),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> List[AlertLogOut]:
    q = db.query(LiquidityAlertHistory)
    if symbol:
        q = q.filter(LiquidityAlertHistory.symbol == symbol.upper())
    if kind:
        q = q.filter(LiquidityAlertHistory.kind == kind)
    if since_ms is not None:
        q = q.filter(LiquidityAlertHistory.started_at_ms >= since_ms)
    rows = q.order_by(LiquidityAlertHistory.started_at_ms.desc()).limit(limit).all()
    return [AlertLogOut(
        alert_id=r.alert_id, symbol=r.symbol, kind=r.kind,
        severity=r.severity, regime=r.regime, confidence=r.confidence,
        priority=r.priority, trigger=r.trigger,
        started_at_ms=r.started_at_ms, last_seen_at_ms=r.last_seen_at_ms,
        validated_outcome=r.validated_outcome,
        validated_at_ms=r.validated_at_ms,
        validation_notes=r.validation_notes,
    ) for r in rows]


# ── Signal stats (per kind) ──────────────────────────────────────────────


# ── Drift series (cohort percentiles over time) ──────────────────────────


# ── Similarity matching ──────────────────────────────────────────────────


# ── Annotations ──────────────────────────────────────────────────────────


class AnnotationIn(BaseModel):
    symbol: str
    ts_ms: int
    kind: str
    note: Optional[str] = None


class AnnotationOut(BaseModel):
    id: int
    symbol: str
    ts_ms: int
    kind: str
    note: Optional[str]
    user_id: Optional[int]
    created_at: float


_ANNOTATION_KINDS = {
    "useful_signal", "false_signal", "manipulation",
    "interesting_setup", "liquidation_event", "spoof_behavior", "other",
}


@router.post("/annotations", response_model=AnnotationOut)
async def add_annotation(
    payload: AnnotationIn = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> AnnotationOut:
    if payload.kind not in _ANNOTATION_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(_ANNOTATION_KINDS)}")
    row = LiquidityAnnotation(
        symbol=payload.symbol.strip().upper(),
        ts_ms=int(payload.ts_ms),
        kind=payload.kind,
        note=payload.note,
        user_id=user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AnnotationOut(
        id=row.id, symbol=row.symbol, ts_ms=row.ts_ms, kind=row.kind,
        note=row.note, user_id=row.user_id,
        created_at=row.created_at.replace(tzinfo=timezone.utc).timestamp(),
    )


# ── Venue quality ────────────────────────────────────────────────────────


# ── Regime timeline reconstructed from alert history ─────────────────────


# ══════════════════════════════════════════════════════════════════════════
#  Phase-8 statistical edge discovery — endpoints
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
#  Phase-9 — Operational Intelligence endpoints
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
#  Phase-10 — Strategic Intelligence endpoints
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
#  Phase-11 — Self-Calibration & Meta-Learning endpoints
# ══════════════════════════════════════════════════════════════════════════


# ── Anomaly memory ────────────────────────────────────────────────────────


# ── Edge mutation ────────────────────────────────────────────────────────


# ── Regime compression ───────────────────────────────────────────────────


# ── Meta-intelligence health ─────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════
#  Phase-12 — Coordination endpoints
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
#  Phase-13 — Market Memory & Evolution endpoints
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
#  Phase-14 — Discovery endpoints
# ══════════════════════════════════════════════════════════════════════════


# ── Phase 18 — Investigation & Casework Layer ────────────────────────


# ── Phase 19 — Replay Intelligence ───────────────────────────────────


# Declared baseline for the value-path probe — NOT live pins, so a pin
# collapse is caught rather than silenced. Mirrors the worker watchdog's
# WATCHDOG_BASELINE default.
LIQ_BASELINE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
# Value-path probe window + freshness/value-change thresholds (display-tier,
# not scientific): GREEN only when credible_depth is genuinely moving.
_VALUE_PROBE_WINDOW_S = 120
_VALUE_PROBE_MAX_AGE_S = 30.0
_VALUE_PROBE_MIN_DV = 2


class ValuePathSymbol(BaseModel):
    symbol: str
    age_s: Optional[float] = None
    dv: int = 0


class ValuePathStatus(BaseModel):
    ok: bool
    per_symbol: List[ValuePathSymbol]
    window_s: int


def _baseline_value_path(db: Session) -> ValuePathStatus:
    """Anti-false-GREEN value-path probe over the declared baseline.

    credible_depth must be genuinely MOVING (value-change), not merely written.
    Shared by /admin/runtime-health and /liquidity/runtime-state — one source of
    truth. `.ok` requires every baseline symbol present AND age_s < 30 AND
    dv >= 2 over the 120s window; missing symbols → age_s=None, dv=0; a DB
    exception is treated as no data (→ not ok), never masked as healthy.
    """
    per_symbol: List[ValuePathSymbol] = []
    try:
        vp_rows = db.execute(
            text(
                """
                SELECT symbol,
                       ((extract(epoch from now()) * 1000)::bigint - max(ts)) / 1000.0 AS age_s,
                       count(DISTINCT value) AS dv
                FROM liquidity_samples
                WHERE metric = 'credible_depth'
                  AND symbol = ANY(:baseline)
                  AND ts > (extract(epoch from now()) * 1000)::bigint - (:win * 1000)
                GROUP BY symbol
                """
            ),
            {"baseline": list(LIQ_BASELINE_SYMBOLS), "win": _VALUE_PROBE_WINDOW_S},
        ).fetchall()
        by_symbol = {r.symbol: r for r in vp_rows}
    except Exception:  # noqa: BLE001
        by_symbol = {}

    for sym in LIQ_BASELINE_SYMBOLS:
        r = by_symbol.get(sym)
        if r is None:
            per_symbol.append(ValuePathSymbol(symbol=sym, age_s=None, dv=0))
        else:
            per_symbol.append(ValuePathSymbol(
                symbol=sym,
                age_s=float(r.age_s) if r.age_s is not None else None,
                dv=int(r.dv or 0),
            ))

    value_path_ok = all(
        p.age_s is not None
        and p.age_s < _VALUE_PROBE_MAX_AGE_S
        and p.dv >= _VALUE_PROBE_MIN_DV
        for p in per_symbol
    )
    return ValuePathStatus(
        ok=value_path_ok, per_symbol=per_symbol, window_s=_VALUE_PROBE_WINDOW_S,
    )


# ── Observation Period runtime-state (Stage 2) ──────────────────────────────
# Read-only operator-visibility endpoint powering the LIQ ObservationBanner.
# T0_NEW is parsed once at import; `Z` is normalised to a tz-aware UTC datetime.
def _parse_t0_new() -> datetime:
    raw = get_settings().t0_new.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_T0_NEW = _parse_t0_new()

# Per-flow chip thresholds (seconds). Display-tier, not scientific.
_FLOW_YELLOW_S = 300    # >= 5 min stale → YELLOW chip / liq_stress soft-warn
_FLOW_RED_S = 900       # >= 15 min stale → RED chip / event-flow soft-warn


class FlowIndicator(BaseModel):
    latest_age_s: Optional[float] = None
    status: str  # "GREEN" | "YELLOW" | "RED"


class RuntimeStateOut(BaseModel):
    t0_new: str
    elapsed_s: float
    hrs_to_3d: float
    hrs_to_7d: float
    baseline: List[str]
    subscribed_count: Optional[int]
    conn_id: Optional[int]
    failure_boundary: str
    health_age_s: Optional[float]
    value_path: ValuePathStatus
    flows: dict  # {name: FlowIndicator}
    continuity: Optional[dict] = None  # computed in a later iteration
    derived_status: str  # "GREEN" | "YELLOW" | "RED"


def _flow_status(age_s: Optional[float]) -> str:
    if age_s is None:
        return "RED"
    if age_s < _FLOW_YELLOW_S:
        return "GREEN"
    if age_s < _FLOW_RED_S:
        return "YELLOW"
    return "RED"


def _latest_age_created_at(db: Session, table: str) -> Optional[float]:
    """Latest-row age (seconds) via PK-desc — O(1). created_at is a SQL
    timestamp, so compare in the timestamp domain (NOT epoch-ms)."""
    row = db.execute(text(
        f"SELECT extract(epoch from (now() - created_at)) AS age_s "
        f"FROM {table} ORDER BY id DESC LIMIT 1"
    )).first()
    return float(row.age_s) if row and row.age_s is not None else None


@router.get("/runtime-state", response_model=RuntimeStateOut)
async def runtime_state_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> RuntimeStateOut:
    """Operator-visibility snapshot for the Observation Period banner. Read-only.

    derived_status RED authority is the value path (Stage-1-aligned); event-like
    flows and failure_boundary are soft (YELLOW at most). Any build failure
    raises 500 — the frontend renders that as a RED banner, never GREEN.
    """
    now_utc = datetime.now(timezone.utc)
    elapsed_s = (now_utc - _T0_NEW).total_seconds()
    hrs_to_3d = max(0.0, 3 * 24 - elapsed_s / 3600.0)
    hrs_to_7d = max(0.0, 7 * 24 - elapsed_s / 3600.0)

    # Latest runtime_health row (timestamp domain for the age).
    h = db.execute(text(
        "SELECT subscribed_count, conn_id, failure_boundary, "
        "extract(epoch from (now() - created_at)) AS age_s "
        "FROM liquidity_runtime_health ORDER BY id DESC LIMIT 1"
    )).first()
    if h is None:
        subscribed_count: Optional[int] = None
        conn_id: Optional[int] = None
        failure_boundary = "UNKNOWN"
        health_age_s: Optional[float] = None
    else:
        subscribed_count = int(h.subscribed_count) if h.subscribed_count is not None else None
        conn_id = int(h.conn_id) if h.conn_id is not None else None
        failure_boundary = h.failure_boundary or "UNKNOWN"
        health_age_s = float(h.age_s) if h.age_s is not None else None

    value_path = _baseline_value_path(db)

    # liq_stress latest age — epoch-ms domain (ts), uses (metric, ts) index.
    # `value IS NOT NULL` lets this use the partial index
    # ix_liq_samples_metric_ts (metric, ts) WHERE value IS NOT NULL — an
    # Index Only Scan instead of a Parallel Seq Scan over the ~66M-row table.
    # liq_stress is written non-null every tick, so max(ts) is unchanged
    # (verified: old_max_ts == new_max_ts).
    ls_row = db.execute(text(
        "SELECT (extract(epoch from now()) * 1000 - max(ts)) / 1000.0 AS age_s "
        "FROM liquidity_samples WHERE metric = 'liq_stress' AND value IS NOT NULL"
    )).first()
    liq_stress_age = float(ls_row.age_s) if ls_row and ls_row.age_s is not None else None

    flows = {
        "bursts": FlowIndicator(
            latest_age_s=(a := _latest_age_created_at(db, "liquidity_bursts")), status=_flow_status(a)),
        "resiliency": FlowIndicator(
            latest_age_s=(a := _latest_age_created_at(db, "liquidity_resiliency")), status=_flow_status(a)),
        "exec_validation": FlowIndicator(
            latest_age_s=(a := _latest_age_created_at(db, "liquidity_exec_validation")), status=_flow_status(a)),
        "liq_stress": FlowIndicator(latest_age_s=liq_stress_age, status=_flow_status(liq_stress_age)),
    }

    baseline = list(LIQ_BASELINE_SYMBOLS)
    # Per-flow chips use their own thresholds. For event-like flows
    # (bursts/resiliency/exec_validation), a RED chip never forces banner RED —
    # value_path is the authoritative sampler-down signal. Do not symmetrize.
    red = (
        h is None
        or not value_path.ok
        or (subscribed_count is not None and subscribed_count < len(baseline))
        or (subscribed_count is None)
    )
    if red:
        derived_status = "RED"
    else:
        yellow = (
            (liq_stress_age is not None and liq_stress_age > _FLOW_YELLOW_S)
            or (liq_stress_age is None)
            or any(
                flows[name].latest_age_s is None or flows[name].latest_age_s >= _FLOW_RED_S
                for name in ("bursts", "resiliency", "exec_validation")
            )
            or failure_boundary != "HEALTHY"
        )
        derived_status = "YELLOW" if yellow else "GREEN"

    return RuntimeStateOut(
        t0_new=get_settings().t0_new,
        elapsed_s=elapsed_s,
        hrs_to_3d=hrs_to_3d,
        hrs_to_7d=hrs_to_7d,
        baseline=baseline,
        subscribed_count=subscribed_count,
        conn_id=conn_id,
        failure_boundary=failure_boundary,
        health_age_s=health_age_s,
        value_path=value_path,
        flows={k: v.model_dump() for k, v in flows.items()},
        continuity=None,
        derived_status=derived_status,
    )
