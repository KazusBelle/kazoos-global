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

    from sqlalchemy import text
    rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (symbol, metric)
                symbol, metric, value, ts
            FROM liquidity_samples
            WHERE symbol = ANY(:symbols) AND ts <= :as_of
            ORDER BY symbol, metric, ts DESC
            """
        ),
        {"symbols": parsed, "as_of": as_of},
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


class SignalKindStats(BaseModel):
    kind: str
    total: int
    resolved: int
    followed_through: int
    noise: int
    pending: int
    precision: Optional[float]      # follow-through / resolved
    false_positive_rate: Optional[float]
    avg_priority: Optional[float]
    avg_confidence: Optional[float]


class SignalStatsResponse(BaseModel):
    since_ms: Optional[int]
    until_ms: Optional[int]
    kinds: List[SignalKindStats]


@router.get("/research/signal-stats", response_model=SignalStatsResponse)
async def signal_stats(
    since_ms: Optional[int] = Query(None),
    until_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SignalStatsResponse:
    from sqlalchemy import func
    q = db.query(LiquidityAlertHistory)
    if since_ms is not None:
        q = q.filter(LiquidityAlertHistory.started_at_ms >= since_ms)
    if until_ms is not None:
        q = q.filter(LiquidityAlertHistory.started_at_ms <= until_ms)

    grouped = (
        q.with_entities(
            LiquidityAlertHistory.kind,
            func.count().label("total"),
            func.avg(LiquidityAlertHistory.priority).label("avg_priority"),
            func.avg(LiquidityAlertHistory.confidence).label("avg_confidence"),
            func.sum(
                case(
                    (LiquidityAlertHistory.validated_outcome == "followed_through", 1),
                    else_=0,
                )
            ).label("followed_through"),
            func.sum(
                case(
                    (LiquidityAlertHistory.validated_outcome == "noise", 1),
                    else_=0,
                )
            ).label("noise"),
            func.sum(
                case(
                    (LiquidityAlertHistory.validated_outcome == "pending", 1),
                    else_=0,
                )
            ).label("pending"),
        )
        .group_by(LiquidityAlertHistory.kind)
        .all()
    )

    kinds: List[SignalKindStats] = []
    for row in grouped:
        ft = int(row.followed_through or 0)
        ns = int(row.noise or 0)
        pn = int(row.pending or 0)
        resolved = ft + ns
        precision = (ft / resolved) if resolved else None
        fpr = (ns / resolved) if resolved else None
        kinds.append(SignalKindStats(
            kind=row.kind,
            total=int(row.total or 0),
            resolved=resolved,
            followed_through=ft,
            noise=ns,
            pending=pn,
            precision=precision,
            false_positive_rate=fpr,
            avg_priority=float(row.avg_priority) if row.avg_priority is not None else None,
            avg_confidence=float(row.avg_confidence) if row.avg_confidence is not None else None,
        ))
    kinds.sort(key=lambda k: k.total, reverse=True)
    return SignalStatsResponse(since_ms=since_ms, until_ms=until_ms, kinds=kinds)


# ── Drift series (cohort percentiles over time) ──────────────────────────


class DriftPoint(BaseModel):
    bucket_ts: int
    p10: Optional[float]
    p50: Optional[float]
    p90: Optional[float]


class DriftSeries(BaseModel):
    metric: str
    bucket_minutes: int
    points: List[DriftPoint]


@router.get("/research/drift", response_model=DriftSeries)
async def drift_series(
    metric: str = Query(...),
    bucket_minutes: int = Query(60, ge=5, le=1440),
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> DriftSeries:
    """Time-bucketed cohort percentiles for a single metric.

    The Research page renders this as a line chart with P10/P50/P90
    overlays — sustained percentile drift is the calibration alarm.
    """
    if metric not in REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown metric: {metric}")
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 7 * 24 * 3600 * 1000

    bucket_ms = bucket_minutes * 60_000
    from sqlalchemy import text
    rows = db.execute(
        text(
            """
            SELECT
              (ts / :bucket_ms) * :bucket_ms AS bucket_ts,
              percentile_disc(0.1) WITHIN GROUP (ORDER BY value) AS p10,
              percentile_disc(0.5) WITHIN GROUP (ORDER BY value) AS p50,
              percentile_disc(0.9) WITHIN GROUP (ORDER BY value) AS p90
            FROM liquidity_samples
            WHERE metric = :metric
              AND ts >= :since_ms
              AND value IS NOT NULL
            GROUP BY 1
            ORDER BY 1
            """
        ),
        {"bucket_ms": bucket_ms, "metric": metric, "since_ms": since_ms},
    ).fetchall()
    points = [
        DriftPoint(
            bucket_ts=int(r.bucket_ts),
            p10=float(r.p10) if r.p10 is not None else None,
            p50=float(r.p50) if r.p50 is not None else None,
            p90=float(r.p90) if r.p90 is not None else None,
        )
        for r in rows
    ]
    return DriftSeries(metric=metric, bucket_minutes=bucket_minutes, points=points)


# ── Similarity matching ──────────────────────────────────────────────────


class SimilarityMatch(BaseModel):
    symbol: str
    ts: int
    distance: float
    metrics: dict[str, float]


class SimilarityResponse(BaseModel):
    symbol: str
    reference_ts: int
    matches: List[SimilarityMatch]


# Metrics that go into the similarity fingerprint. Lightweight set —
# adding more dimensions would just make L2 distances asymptote to the
# same big number for every candidate.
_SIM_METRICS = (
    "spread", "credible_depth", "obi", "liq_stress",
    "funding_z", "oi_delta_1h", "fragility_score", "resiliency_score",
)


@router.get("/research/similarity/{symbol}", response_model=SimilarityResponse)
async def find_similar(
    symbol: str,
    top_k: int = Query(10, ge=1, le=50),
    sample_minutes: int = Query(15, ge=1, le=240,
                                description="Spacing between candidate timestamps"),
    lookback_days: int = Query(14, ge=1, le=60),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SimilarityResponse:
    """Find the historical states most similar to `symbol`'s current state.

    We sample one fingerprint per `sample_minutes` window so we don't
    compare against every single 60s tick — the resulting candidate set
    is small enough to score in Python without a vector index. L2
    distance in the normalized metric space.
    """
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol required")

    now_ms = int(time.time() * 1000)
    since_ms = now_ms - lookback_days * 24 * 3600 * 1000
    sample_ms = sample_minutes * 60_000

    from sqlalchemy import text
    # Reference fingerprint = latest non-null value per metric for this symbol.
    ref_rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (metric) metric, value, ts
            FROM liquidity_samples
            WHERE symbol = :sym
              AND metric = ANY(:metrics)
              AND value IS NOT NULL
            ORDER BY metric, ts DESC
            """
        ),
        {"sym": sym, "metrics": list(_SIM_METRICS)},
    ).fetchall()

    if not ref_rows:
        return SimilarityResponse(symbol=sym, reference_ts=now_ms, matches=[])

    reference: dict[str, float] = {r.metric: float(r.value) for r in ref_rows}
    ref_ts = max(int(r.ts) for r in ref_rows)

    # Candidate fingerprints: one per (symbol, sampled bucket). To keep
    # the query bounded we restrict to the same symbol and to bucketed
    # windows — cross-symbol matching is a future extension.
    cand_rows = db.execute(
        text(
            """
            SELECT symbol, metric, value, (ts / :sample_ms) * :sample_ms AS bucket_ts
            FROM liquidity_samples
            WHERE symbol = :sym
              AND metric = ANY(:metrics)
              AND ts >= :since_ms
              AND ts <= :until_ms
              AND value IS NOT NULL
            ORDER BY bucket_ts, metric, ts
            """
        ),
        {
            "sample_ms": sample_ms,
            "sym": sym,
            "metrics": list(_SIM_METRICS),
            "since_ms": since_ms,
            "until_ms": now_ms - sample_ms,  # exclude the reference window itself
        },
    ).fetchall()

    # Group into per-bucket fingerprints. Last-wins per (bucket, metric).
    fingerprints: dict[int, dict[str, float]] = {}
    for r in cand_rows:
        fingerprints.setdefault(int(r.bucket_ts), {})[r.metric] = float(r.value)

    # Cohort scale: use the reference value's order of magnitude per
    # metric for normalization so funding (1e-4) doesn't get dwarfed by
    # credible_depth (1e7). One-shot global scales would be more
    # principled but materially more expensive; reference-relative
    # scaling is "good enough" for top-K ranking.
    scales = {m: max(abs(v), 1e-6) for m, v in reference.items()}

    scored: List[tuple[int, float, dict[str, float]]] = []
    for ts_bucket, vec in fingerprints.items():
        # Require ≥ half the metrics to be present in the fingerprint.
        if len(vec) < max(2, len(reference) // 2):
            continue
        ssq = 0.0
        for m, ref_v in reference.items():
            v = vec.get(m)
            if v is None:
                continue
            scale = scales[m]
            ssq += ((v - ref_v) / scale) ** 2
        dist = ssq ** 0.5
        scored.append((ts_bucket, dist, vec))

    scored.sort(key=lambda x: x[1])
    matches = [
        SimilarityMatch(symbol=sym, ts=ts, distance=dist, metrics=vec)
        for ts, dist, vec in scored[:top_k]
    ]
    return SimilarityResponse(symbol=sym, reference_ts=ref_ts, matches=matches)


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


@router.delete("/annotations/{annotation_id}", status_code=204, response_class=Response)
async def delete_annotation(
    annotation_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    row = db.query(LiquidityAnnotation).filter(LiquidityAnnotation.id == annotation_id).first()
    if row is None:
        return Response(status_code=204)
    db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.get("/annotations", response_model=List[AnnotationOut])
async def list_annotations(
    symbol: Optional[str] = Query(None),
    since_ms: Optional[int] = Query(None),
    limit: int = Query(200, le=2000),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> List[AnnotationOut]:
    q = db.query(LiquidityAnnotation)
    if symbol:
        q = q.filter(LiquidityAnnotation.symbol == symbol.upper())
    if since_ms is not None:
        q = q.filter(LiquidityAnnotation.ts_ms >= since_ms)
    rows = q.order_by(LiquidityAnnotation.ts_ms.desc()).limit(limit).all()
    return [AnnotationOut(
        id=r.id, symbol=r.symbol, ts_ms=r.ts_ms, kind=r.kind,
        note=r.note, user_id=r.user_id,
        created_at=r.created_at.replace(tzinfo=timezone.utc).timestamp(),
    ) for r in rows]


# ── Venue quality ────────────────────────────────────────────────────────


class VenueStats(BaseModel):
    exchange: str
    samples: int
    avg_spread_bps: Optional[float]
    avg_oi_usd: Optional[float]
    median_funding_bps: Optional[float]
    # Pairwise vs binance reference — populated for non-binance venues.
    avg_mid_divergence_pct: Optional[float] = None
    avg_funding_divergence_bps: Optional[float] = None


class VenueQualityResponse(BaseModel):
    since_ms: int
    venues: List[VenueStats]


@router.get("/research/venue-quality", response_model=VenueQualityResponse)
async def venue_quality(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> VenueQualityResponse:
    from sqlalchemy import func

    if since_ms is None:
        since_ms = int(time.time() * 1000) - 7 * 24 * 3600 * 1000

    # Mixing percentile_disc through the ORM is brittle across SA versions;
    # the venue-quality query is small, so we go straight to text.
    base = db.execute(
        text(
            """
            SELECT
              exchange,
              COUNT(*) AS samples,
              AVG(spread_fraction) AS avg_spread,
              AVG(open_interest_usd) AS avg_oi,
              percentile_disc(0.5) WITHIN GROUP (ORDER BY funding_rate) AS median_funding
            FROM liquidity_crossex_history
            WHERE ts_ms >= :since_ms
            GROUP BY exchange
            """
        ),
        {"since_ms": since_ms},
    ).fetchall()

    # Divergence vs binance reference. We don't pre-store the diff so we
    # join the table to itself on (symbol, bucketed_ts).
    div_rows = db.execute(
        text(
            """
            WITH ref AS (
              SELECT symbol, ts_ms, mid_price, funding_rate
              FROM liquidity_crossex_history
              WHERE exchange = 'binance' AND ts_ms >= :since_ms
            ),
            other AS (
              SELECT symbol, ts_ms, exchange, mid_price, funding_rate
              FROM liquidity_crossex_history
              WHERE exchange != 'binance' AND ts_ms >= :since_ms
            )
            SELECT
              other.exchange,
              AVG(ABS(other.mid_price - ref.mid_price) / NULLIF(ref.mid_price, 0)) * 100 AS mid_div_pct,
              AVG(ABS(other.funding_rate - ref.funding_rate)) * 10000 AS funding_div_bps
            FROM other
            JOIN ref ON other.symbol = ref.symbol
                    AND ABS(other.ts_ms - ref.ts_ms) < 60000
            GROUP BY other.exchange
            """
        ),
        {"since_ms": since_ms},
    ).fetchall()

    div_by_ex = {d.exchange: (d.mid_div_pct, d.funding_div_bps) for d in div_rows}

    out: List[VenueStats] = []
    for r in base:
        mid_div, fund_div = div_by_ex.get(r.exchange, (None, None))
        out.append(VenueStats(
            exchange=r.exchange,
            samples=int(r.samples or 0),
            avg_spread_bps=(float(r.avg_spread) * 10_000) if r.avg_spread is not None else None,
            avg_oi_usd=float(r.avg_oi) if r.avg_oi is not None else None,
            median_funding_bps=(float(r.median_funding) * 10_000) if r.median_funding is not None else None,
            avg_mid_divergence_pct=float(mid_div) if mid_div is not None else None,
            avg_funding_divergence_bps=float(fund_div) if fund_div is not None else None,
        ))
    out.sort(key=lambda v: v.samples, reverse=True)
    return VenueQualityResponse(since_ms=since_ms, venues=out)


# ── Regime timeline reconstructed from alert history ─────────────────────


class RegimeTransition(BaseModel):
    from_regime: str
    to_regime: str
    count: int


class RegimeStatsResponse(BaseModel):
    since_ms: int
    regime_counts: dict[str, int]
    top_transitions: List[RegimeTransition]


@router.get("/research/regime-stats", response_model=RegimeStatsResponse)
async def regime_stats(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> RegimeStatsResponse:
    """Per-regime counts + most-common transitions, derived from the alert
    history's `regime` column. Without a dedicated regime_history table
    we use alert occurrences as a proxy for regime presence — coarse but
    sufficient to see which regimes dominate and how the market shifts."""
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000

    from sqlalchemy import func
    counts = (
        db.query(LiquidityAlertHistory.regime, func.count().label("c"))
        .filter(LiquidityAlertHistory.started_at_ms >= since_ms)
        .group_by(LiquidityAlertHistory.regime)
        .all()
    )
    regime_counts = {r.regime: int(r.c) for r in counts}

    # Transitions: for each symbol, walk alerts in chronological order
    # and count regime → regime changes. Pull every relevant row and
    # process in Python — easier than window functions, dataset is small.
    rows = (
        db.query(LiquidityAlertHistory.symbol, LiquidityAlertHistory.regime,
                 LiquidityAlertHistory.started_at_ms)
        .filter(LiquidityAlertHistory.started_at_ms >= since_ms)
        .order_by(LiquidityAlertHistory.symbol.asc(),
                  LiquidityAlertHistory.started_at_ms.asc())
        .all()
    )
    transitions: dict[tuple[str, str], int] = {}
    last_by_sym: dict[str, str] = {}
    for r in rows:
        prev = last_by_sym.get(r.symbol)
        if prev is not None and prev != r.regime:
            key = (prev, r.regime)
            transitions[key] = transitions.get(key, 0) + 1
        last_by_sym[r.symbol] = r.regime

    top = sorted(transitions.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return RegimeStatsResponse(
        since_ms=since_ms,
        regime_counts=regime_counts,
        top_transitions=[
            RegimeTransition(from_regime=a, to_regime=b, count=c)
            for (a, b), c in top
        ],
    )


# ══════════════════════════════════════════════════════════════════════════
#  Phase-8 statistical edge discovery — endpoints
# ══════════════════════════════════════════════════════════════════════════

from kazus_logic.liquidity import research as _research


class InteractionCell(BaseModel):
    a: str
    b: str
    r: Optional[float]
    n: int


class InteractionMatrixOut(BaseModel):
    metrics: List[str]
    cells: List[InteractionCell]
    since_ms: int
    bucket_minutes: int


@router.get("/research/interactions", response_model=InteractionMatrixOut)
async def interactions(
    since_ms: Optional[int] = Query(None),
    bucket_minutes: int = Query(5, ge=1, le=60),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> InteractionMatrixOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 14 * 24 * 3600 * 1000
    data = _research.interaction_matrix(db, since_ms=since_ms, bucket_minutes=bucket_minutes)
    return InteractionMatrixOut(**data)


class EdgeComboRow(BaseModel):
    resiliency: str
    fragility: str
    funding_z: str
    total: int
    outcomes: int
    rate: float
    lift: Optional[float]


class EdgeRankingOut(BaseModel):
    since_ms: int
    bucket_minutes: int
    alert_kind: Optional[str]
    metrics: List[str]
    tertile_cuts: dict
    total_buckets: int
    base_rate: float
    outcome_window_ms: int
    combos: List[EdgeComboRow]


@router.get("/research/edge-ranking", response_model=EdgeRankingOut)
async def edge_ranking(
    alert_kind: Optional[str] = Query(None, description="Restrict outcome to this alert kind"),
    since_ms: Optional[int] = Query(None),
    bucket_minutes: int = Query(15, ge=5, le=60),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> EdgeRankingOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 14 * 24 * 3600 * 1000
    data = _research.edge_ranking(
        db, since_ms=since_ms, alert_kind=alert_kind, bucket_minutes=bucket_minutes,
    )
    return EdgeRankingOut(**data)


class RegimeOutcomeRow(BaseModel):
    regime: str
    count: int
    out_transitions: int
    transitions: dict
    collapse_prob: float
    avg_duration_ms: Optional[float]


class RegimeOutcomesOut(BaseModel):
    since_ms: int
    regimes: List[RegimeOutcomeRow]
    transition_pairs: List[RegimeTransition]


@router.get("/research/regime-outcomes", response_model=RegimeOutcomesOut)
async def regime_outcomes(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> RegimeOutcomesOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000
    data = _research.regime_outcomes(db, since_ms=since_ms)
    return RegimeOutcomesOut(
        since_ms=data["since_ms"],
        regimes=[RegimeOutcomeRow(**r) for r in data["regimes"]],
        transition_pairs=[RegimeTransition(**t) for t in data["transition_pairs"]],
    )


class VenueLagRow(BaseModel):
    exchange: str
    samples: int
    mean_lag_s: float
    share_led_binance: float
    share_lagged_binance: float


class VenueLagMetric(BaseModel):
    metric: str
    venues: List[VenueLagRow]


class VenueLeadershipOut(BaseModel):
    since_ms: int
    pair_count: int
    max_lag_s: int
    metrics: List[VenueLagMetric]


@router.get("/research/venue-leadership", response_model=VenueLeadershipOut)
async def venue_leadership(
    since_ms: Optional[int] = Query(None),
    max_lag_s: int = Query(120, ge=15, le=600),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> VenueLeadershipOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 14 * 24 * 3600 * 1000
    data = _research.venue_leadership(db, since_ms=since_ms, max_lag_s=max_lag_s)
    return VenueLeadershipOut(
        since_ms=data["since_ms"],
        pair_count=data["pair_count"],
        max_lag_s=data["max_lag_s"],
        metrics=[
            VenueLagMetric(
                metric=m["metric"],
                venues=[VenueLagRow(**v) for v in m["venues"]],
            )
            for m in data["metrics"]
        ],
    )


class MetaStateOut(BaseModel):
    symbol: str
    rarity_pct: Optional[float]
    n: int
    similar_count: Optional[int] = None
    threshold: Optional[float] = None
    reference: Optional[dict] = None


@router.get("/research/meta-state/{symbol}", response_model=MetaStateOut)
async def meta_state(
    symbol: str,
    lookback_days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MetaStateOut:
    data = _research.meta_state_rarity(db, symbol=symbol, lookback_days=lookback_days)
    return MetaStateOut(**data)


# ══════════════════════════════════════════════════════════════════════════
#  Phase-9 — Operational Intelligence endpoints
# ══════════════════════════════════════════════════════════════════════════


class EdgePersistenceBucket(BaseModel):
    bucket_ts: int
    total: int
    resolved: int
    precision: Optional[float]
    avg_priority: Optional[float]
    avg_confidence: Optional[float]


class EdgePersistenceKind(BaseModel):
    kind: str
    series: List[EdgePersistenceBucket]
    slope_per_window: Optional[float]
    slope_per_day: Optional[float]
    intercept: Optional[float]
    half_life_days: Optional[float]
    latest_precision: Optional[float]


class EdgePersistenceOut(BaseModel):
    since_ms: int
    window_days: int
    alert_kind: Optional[str]
    kinds: List[EdgePersistenceKind]


@router.get("/research/edge-persistence", response_model=EdgePersistenceOut)
async def edge_persistence(
    since_ms: Optional[int] = Query(None),
    window_days: int = Query(7, ge=1, le=30),
    alert_kind: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> EdgePersistenceOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 60 * 24 * 3600 * 1000
    data = _research.edge_persistence(
        db, since_ms=since_ms, window_days=window_days, alert_kind=alert_kind,
    )
    return EdgePersistenceOut(**data)


class ReliabilityComponents(BaseModel):
    accuracy: float
    stability: float
    regime_consistency: float
    sample_size: float


class ReliabilityKind(BaseModel):
    kind: str
    total: int
    resolved: int
    accuracy: float
    weekly_buckets: int
    weekly_precision_std: float
    regime_buckets: int
    regime_precision_std: float
    components: ReliabilityComponents
    reliability_score: float
    state: str
    rank: int


class ReliabilityOut(BaseModel):
    since_ms: int
    kinds: List[ReliabilityKind]


@router.get("/research/signal-reliability", response_model=ReliabilityOut)
async def signal_reliability(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ReliabilityOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000
    data = _research.signal_reliability(db, since_ms=since_ms)
    return ReliabilityOut(**data)


class TransitionForecastRegime(BaseModel):
    regime: str
    count: int
    out_transitions: int
    next_probs: dict
    expected_latency_ms: Optional[float]
    median_latency_ms: Optional[float]
    collapse_prob: float
    stabilization_prob: float
    volatility_expansion_prob: float


class TransitionForecastOut(BaseModel):
    since_ms: int
    regimes: List[TransitionForecastRegime]


@router.get("/research/transition-forecast", response_model=TransitionForecastOut)
async def transition_forecast(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> TransitionForecastOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000
    data = _research.transition_forecast(db, since_ms=since_ms)
    return TransitionForecastOut(**data)


class RiskInstabilityRow(BaseModel):
    symbol: str
    stress: float
    components: dict


class RiskStateOut(BaseModel):
    risk_state_score: float
    systemic_stress_level: str   # QUIET | WATCH | ELEVATED | SEVERE
    n_symbols: int
    drivers: dict
    instability_rank: List[RiskInstabilityRow]


@router.get("/research/risk-state", response_model=RiskStateOut)
async def risk_state_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> RiskStateOut:
    return RiskStateOut(**_research.risk_state(db))


class NarrativeAlertKind(BaseModel):
    kind: str
    count: int


class NarrativeRegime(BaseModel):
    regime: str
    count: int


class MarketNarrativeOut(BaseModel):
    headline: str
    score: float
    level: str
    bullets: List[str]
    alert_summary: str
    regime_summary: str
    historical_context: str
    top_alert_kinds: List[NarrativeAlertKind]
    top_regimes: List[NarrativeRegime]
    fetched_at_ms: int


@router.get("/research/market-narrative", response_model=MarketNarrativeOut)
async def market_narrative_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MarketNarrativeOut:
    return MarketNarrativeOut(**_research.market_narrative(db))


# ══════════════════════════════════════════════════════════════════════════
#  Phase-10 — Strategic Intelligence endpoints
# ══════════════════════════════════════════════════════════════════════════


class CorrDrift(BaseModel):
    a: str; b: str
    r_prev: Optional[float]
    r_cur: Optional[float]
    delta: float


class MetricMigration(BaseModel):
    metric: str
    prev_median: float
    cur_median: float
    pct_delta: float


class RegimeShareDrift(BaseModel):
    regime: str
    prev_share: float
    cur_share: float
    delta: float


class StructuralBreakOut(BaseModel):
    window_days: int
    structural_break_score: float
    break_confidence: float
    components: dict
    affected_correlations: List[CorrDrift]
    affected_metrics: List[MetricMigration]
    affected_regimes: List[RegimeShareDrift]
    cur_since: int
    prev_since: int


@router.get("/research/structural-breaks", response_model=StructuralBreakOut)
async def structural_breaks_endpoint(
    window_days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> StructuralBreakOut:
    return StructuralBreakOut(**_research.structural_breaks(db, window_days=window_days))


class RegimeShiftSignal(BaseModel):
    name: str
    value: float


class RegimeShiftWarningOut(BaseModel):
    fetched_at_ms: int
    regime_shift_probability: float
    instability_acceleration: float
    warning_state: str       # STABLE | WATCH | ELEVATED_TRANSITION_RISK | PRE_CASCADE | INSUFFICIENT_DATA
    signals: List[RegimeShiftSignal]


@router.get("/research/regime-shift-warning", response_model=RegimeShiftWarningOut)
async def regime_shift_warning_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> RegimeShiftWarningOut:
    return RegimeShiftWarningOut(**_research.regime_shift_warning(db))


class AdaptiveReliabilityKind(BaseModel):
    kind: str
    total: int
    resolved: int
    accuracy: float
    weekly_buckets: int
    weekly_precision_std: float
    regime_buckets: int
    regime_precision_std: float
    components: ReliabilityComponents
    reliability_score: float
    state: str
    rank: int
    regime_multiplier: float
    regime_adjusted_reliability: float
    adjusted_rank: int


class AdaptiveReliabilityOut(BaseModel):
    since_ms: int
    dominant_regime: str
    kinds: List[AdaptiveReliabilityKind]


@router.get("/research/adaptive-reliability", response_model=AdaptiveReliabilityOut)
async def adaptive_reliability_endpoint(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AdaptiveReliabilityOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000
    return AdaptiveReliabilityOut(**_research.adaptive_reliability(db, since_ms=since_ms))


class MetaConfidenceComponents(BaseModel):
    avg_confidence: float
    noise_resistance: float
    regime_jitter: float
    structural_break_inv: float


class MetaConfidenceOut(BaseModel):
    meta_confidence_score: float
    confidence_stability: float
    trustworthiness_state: str   # TRUSTWORTHY | GUARDED | UNRELIABLE | UNKNOWN
    components: Optional[MetaConfidenceComponents] = None
    n_alerts: int
    noise_rate: Optional[float] = None
    avg_distinct_regimes: Optional[float] = None


@router.get("/research/meta-confidence", response_model=MetaConfidenceOut)
async def meta_confidence_endpoint(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MetaConfidenceOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000
    data = _research.meta_confidence(db, since_ms=since_ms)
    # `components` is empty dict when there are no alerts — flatten that.
    comps = data.get("components") or None
    if comps and isinstance(comps, dict) and comps:
        data["components"] = MetaConfidenceComponents(**comps)
    else:
        data["components"] = None
    return MetaConfidenceOut(**data)


class SurvivalPoint(BaseModel):
    ts: int
    s: float


class EdgeSurvivalKind(BaseModel):
    kind: str
    deaths: int
    expected_remaining_days: Optional[float]
    degradation_acceleration: Optional[float]
    latest_precision: Optional[float]
    threshold: float
    survival_curve: List[SurvivalPoint]


class EdgeSurvivalOut(BaseModel):
    since_ms: int
    threshold: float
    kinds: List[EdgeSurvivalKind]


@router.get("/research/edge-survival", response_model=EdgeSurvivalOut)
async def edge_survival_endpoint(
    since_ms: Optional[int] = Query(None),
    threshold: float = Query(0.5, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> EdgeSurvivalOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 60 * 24 * 3600 * 1000
    return EdgeSurvivalOut(**_research.edge_survival(db, since_ms=since_ms, threshold=threshold))


class EvolutionPoint(BaseModel):
    ts: int
    v: float


class EvolutionMetric(BaseModel):
    metric: str
    series: List[EvolutionPoint]
    slope_per_day: Optional[float]


class EntropyPoint(BaseModel):
    ts: int
    entropy: float
    dominant_regime: str


class MarketEvolutionOut(BaseModel):
    lookback_days: int
    bucket_days: int
    metric_trends: List[EvolutionMetric]
    regime_entropy_series: List[EntropyPoint]


@router.get("/research/market-evolution", response_model=MarketEvolutionOut)
async def market_evolution_endpoint(
    lookback_days: int = Query(60, ge=14, le=180),
    bucket_days: int = Query(7, ge=1, le=14),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MarketEvolutionOut:
    return MarketEvolutionOut(**_research.market_evolution(
        db, lookback_days=lookback_days, bucket_days=bucket_days,
    ))


class StrategicStateOut(BaseModel):
    state: str         # STABLE_INSTITUTIONAL_FLOW | FRAGILE_SPECULATIVE_MARKET | TRANSITIONAL_UNSTABLE | CASCADE_RISK_ENVIRONMENT | LIQUIDITY_DETERIORATION_PHASE
    trustworthiness: str
    rationale: List[str]
    inputs: dict


@router.get("/research/strategic-state", response_model=StrategicStateOut)
async def strategic_state_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> StrategicStateOut:
    return StrategicStateOut(**_research.strategic_state(db))


# ══════════════════════════════════════════════════════════════════════════
#  Phase-11 — Self-Calibration & Meta-Learning endpoints
# ══════════════════════════════════════════════════════════════════════════


class ThresholdCalibrationKind(BaseModel):
    kind: str
    total: int
    resolved: int
    precision: Optional[float]
    per_day: float
    action: str   # TIGHTEN | LOOSEN | HOLD
    adjustment_multiplier: float
    calibration_confidence: float
    rationale: List[str]


class ThresholdCalibrationOut(BaseModel):
    since_ms: int
    kinds: List[ThresholdCalibrationKind]


@router.get("/research/threshold-calibration", response_model=ThresholdCalibrationOut)
async def threshold_calibration_endpoint(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ThresholdCalibrationOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000
    return ThresholdCalibrationOut(**_research.threshold_calibration(db, since_ms=since_ms))


class MetricWeight(BaseModel):
    metric: str
    samples: int
    extreme_hits: int
    extreme_share: float
    relevance_score: float
    weight: float


class AdaptiveWeightsOut(BaseModel):
    since_ms: int
    weights: List[MetricWeight]


@router.get("/research/adaptive-metric-weights", response_model=AdaptiveWeightsOut)
async def adaptive_metric_weights_endpoint(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AdaptiveWeightsOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 7 * 24 * 3600 * 1000
    return AdaptiveWeightsOut(**_research.adaptive_metric_weights(db, since_ms=since_ms))


class StateEmbeddingOut(BaseModel):
    metrics: List[str]
    fingerprint: dict
    ts_ms: int


@router.get("/research/state-embedding", response_model=StateEmbeddingOut)
async def state_embedding_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> StateEmbeddingOut:
    return StateEmbeddingOut(**_research.state_embedding(db))


# ── Anomaly memory ────────────────────────────────────────────────────────


_ANOMALY_KINDS = {
    "structural_break", "regime_collapse", "venue_divergence",
    "pre_cascade", "edge_inversion", "regime_emergence",
}


class AnomalyRecordIn(BaseModel):
    kind: str
    severity: str = "info"
    fingerprint: dict
    related_alert_ids: Optional[List[str]] = None
    notes: Optional[str] = None


class AnomalyRecordOut(BaseModel):
    id: int
    kind: str
    severity: str
    occurred_at_ms: int
    novelty_score: float
    recurrence_count: int
    fingerprint: dict
    best_match_id: Optional[int]
    best_match_distance: Optional[float]


@router.post("/research/anomaly-memory", response_model=AnomalyRecordOut)
async def record_anomaly_endpoint(
    payload: AnomalyRecordIn = Body(...),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AnomalyRecordOut:
    if payload.kind not in _ANOMALY_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {sorted(_ANOMALY_KINDS)}",
        )
    data = _research.record_anomaly(
        db,
        kind=payload.kind,
        severity=payload.severity,
        fingerprint=payload.fingerprint,
        related_alert_ids=payload.related_alert_ids,
        notes=payload.notes,
    )
    return AnomalyRecordOut(**data)


class AnomalyMemoryItem(BaseModel):
    id: int
    kind: str
    severity: str
    occurred_at_ms: int
    fingerprint: dict
    novelty_score: float
    recurrence_count: int
    notes: Optional[str]


class AnomalyMemoryOut(BaseModel):
    items: List[AnomalyMemoryItem]
    counts_by_kind: dict


@router.get("/research/anomaly-memory", response_model=AnomalyMemoryOut)
async def list_anomaly_memory(
    kind: Optional[str] = Query(None),
    since_ms: Optional[int] = Query(None),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AnomalyMemoryOut:
    return AnomalyMemoryOut(**_research.query_anomaly_memory(
        db, kind=kind, since_ms=since_ms, limit=limit,
    ))


# ── Edge mutation ────────────────────────────────────────────────────────


class EdgeMutationKind(BaseModel):
    kind: str
    recent_precision: Optional[float]
    prior_precision: Optional[float]
    recent_resolved: int
    prior_resolved: int
    delta: Optional[float]
    mutation_velocity_per_day: Optional[float]
    mutation_score: float
    mutation_direction: str    # STRENGTHENING | WEAKENING | INVERTED | NEUTRAL
    inverted: bool


class EdgeMutationOut(BaseModel):
    since_ms: int
    window_days: int
    kinds: List[EdgeMutationKind]


@router.get("/research/edge-mutation", response_model=EdgeMutationOut)
async def edge_mutation_endpoint(
    since_ms: Optional[int] = Query(None),
    window_days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> EdgeMutationOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 2 * window_days * 24 * 3600 * 1000
    return EdgeMutationOut(**_research.edge_mutation(db, since_ms=since_ms, window_days=window_days))


# ── Regime compression ───────────────────────────────────────────────────


class RegimeCompressionCell(BaseModel):
    a: str; b: str
    cosine: float
    distance: float


class RegimeCompressionMerge(BaseModel):
    a: str; b: str
    cosine: float


class RegimeCompressionOut(BaseModel):
    since_ms: int
    regimes: List[str]
    matrix: List[RegimeCompressionCell]
    merge_candidates: List[RegimeCompressionMerge]


@router.get("/research/regime-compression", response_model=RegimeCompressionOut)
async def regime_compression_endpoint(
    since_ms: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> RegimeCompressionOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 30 * 24 * 3600 * 1000
    return RegimeCompressionOut(**_research.regime_compression(db, since_ms=since_ms))


# ── Meta-intelligence health ─────────────────────────────────────────────


class MetaHealthComponents(BaseModel):
    meta_confidence: float
    structural_stability: float
    alert_saturation: float
    edge_consistency: float
    regime_focus: float


class MetaHealthOut(BaseModel):
    meta_intelligence_health: float
    state: str    # HEALTHY | DRIFTING | DEGRADING | CRITICAL
    self_consistency_score: float
    adaptation_quality: float
    components: MetaHealthComponents
    alert_saturation_ratio: float
    avg_distinct_regimes_per_day: float
    mutation_magnitude_sum: float


@router.get("/research/meta-intelligence-health", response_model=MetaHealthOut)
async def meta_health_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MetaHealthOut:
    return MetaHealthOut(**_research.meta_intelligence_health(db))


# ══════════════════════════════════════════════════════════════════════════
#  Phase-12 — Coordination endpoints
# ══════════════════════════════════════════════════════════════════════════


class SynthesisLayer(BaseModel):
    name: str
    score: float
    weight: float
    delta_from_mean: float


class SynthesisComponents(BaseModel):
    stress_level: str
    shift_warning: str
    structural_break_score: float
    meta_confidence_state: str
    intelligence_health_state: str
    strategic_state: str


class SynthesisOut(BaseModel):
    fetched_at_ms: int
    synthesized_stress: float
    coordinated_state: str
    cross_layer_agreement: float
    layers: List[SynthesisLayer]
    components: SynthesisComponents


@router.get("/research/synthesis", response_model=SynthesisOut)
async def synthesis_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SynthesisOut:
    return SynthesisOut(**_research.intelligence_synthesis(db))


class ConflictItem(BaseModel):
    kind: str
    description: str
    ops_score: Optional[float] = None
    structural_score: Optional[float] = None
    dominant_horizon: Optional[str] = None
    shift_probability: Optional[float] = None
    confidence_deficit: Optional[float] = None


class ConflictsOut(BaseModel):
    fetched_at_ms: int
    conflict_score: float
    conflicts: List[ConflictItem]
    dominant_layer: str
    suppressed_layers: List[str]


@router.get("/research/conflicts", response_model=ConflictsOut)
async def conflicts_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ConflictsOut:
    return ConflictsOut(**_research.intelligence_conflicts(db))


class SuppressionCluster(BaseModel):
    cluster_id: int
    symbol: str
    kind: str
    count: int
    max_severity: str
    last_seen_ms: int
    redundancy_score: float


class SuppressionOut(BaseModel):
    window_minutes: int
    total_alerts: int
    unique_clusters: int
    alert_compression_ratio: float
    redundant_clusters: List[SuppressionCluster]


@router.get("/research/alert-suppression", response_model=SuppressionOut)
async def suppression_endpoint(
    window_minutes: int = Query(60, ge=5, le=720),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SuppressionOut:
    return SuppressionOut(**_research.alert_suppression(db, window_minutes=window_minutes))


class CrisisCluster(BaseModel):
    cluster_id: int
    size: int
    dominant_kind: str
    kinds: dict
    frequency_per_day: float
    earliest_ts: int
    latest_ts: int
    avg_novelty: float
    centroid: dict
    recent_members: List[dict]


class CrisisClustersOut(BaseModel):
    clusters: List[CrisisCluster]
    anomaly_count: int


@router.get("/research/crisis-clusters", response_model=CrisisClustersOut)
async def crisis_clusters_endpoint(
    max_clusters: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CrisisClustersOut:
    return CrisisClustersOut(**_research.crisis_clusters(db, max_clusters=max_clusters))


class HorizonInfo(BaseModel):
    label: str
    window_ms: int


class NarrativeMetricChange(BaseModel):
    metric: str
    h1: Optional[float]
    h24: Optional[float]
    d7: Optional[float]
    change_1h_vs_24h_pct: Optional[float]
    change_24h_vs_7d_pct: Optional[float]


class NarrativeEvolutionOut(BaseModel):
    fetched_at_ms: int
    horizons: List[HorizonInfo]
    metric_changes: List[NarrativeMetricChange]
    short_term_bullets: List[str]
    long_term_bullets: List[str]


@router.get("/research/narrative-evolution", response_model=NarrativeEvolutionOut)
async def narrative_evolution_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> NarrativeEvolutionOut:
    return NarrativeEvolutionOut(**_research.narrative_evolution(db))


class MultiHorizonOut(BaseModel):
    fetched_at_ms: int
    scores: dict
    horizon_alignment_score: Optional[float]
    horizon_conflict_map: dict
    dominant_horizon: Optional[str]
    structural_alignment_state: str


@router.get("/research/multi-horizon", response_model=MultiHorizonOut)
async def multi_horizon_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MultiHorizonOut:
    return MultiHorizonOut(**_research.multi_horizon(db))


class AutoAnomalyInserted(BaseModel):
    id: int
    kind: str
    severity: str
    occurred_at_ms: int
    novelty_score: float
    recurrence_count: int


class AutoAnomalyDecision(BaseModel):
    kind: str
    action: str
    score: Optional[float] = None
    state: Optional[str] = None
    error: Optional[str] = None
    regime: Optional[str] = None
    alert_kind: Optional[str] = None
    pct: Optional[float] = None
    next_eligible_in_ms: Optional[int] = None


class AutoAnomalyOut(BaseModel):
    fetched_at_ms: int
    inserted: List[AutoAnomalyInserted]
    decisions: List[AutoAnomalyDecision]


@router.post("/research/auto-anomaly-scan", response_model=AutoAnomalyOut)
async def auto_anomaly_scan_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AutoAnomalyOut:
    """Run one auto-anomaly scan on-demand. The worker calls the same
    function on a 5-min cadence; this endpoint exists so the UI can
    trigger a refresh after a manual change or for debugging."""
    data = _research.auto_record_anomalies(db)
    # Strip fingerprints from inserted rows for response brevity.
    inserted = [
        AutoAnomalyInserted(
            id=row["id"], kind=row["kind"], severity=row["severity"],
            occurred_at_ms=row["occurred_at_ms"],
            novelty_score=row["novelty_score"],
            recurrence_count=row["recurrence_count"],
        )
        for row in data.get("inserted", [])
    ]
    return AutoAnomalyOut(
        fetched_at_ms=data["fetched_at_ms"],
        inserted=inserted,
        decisions=[AutoAnomalyDecision(**d) for d in data.get("decisions", [])],
    )


# ══════════════════════════════════════════════════════════════════════════
#  Phase-13 — Market Memory & Evolution endpoints
# ══════════════════════════════════════════════════════════════════════════


class AnomalyNode(BaseModel):
    id: int
    kind: str
    severity: str
    occurred_at_ms: int
    novelty_score: float
    recurrence_count: int
    fingerprint: dict


class AnomalyEdge(BaseModel):
    from_id: int
    to_id: int
    kind: str
    weight: float
    depth: Optional[int] = None


class AnomalyLineageOut(BaseModel):
    id: int
    root: Optional[AnomalyNode]
    parents: List[List[AnomalyNode]]
    descendants: List[List[AnomalyNode]]
    edges: List[AnomalyEdge]
    lineage_depth: int
    neighborhood_size: int


@router.get("/research/anomaly-lineage/{anomaly_id}", response_model=AnomalyLineageOut)
async def anomaly_lineage_endpoint(
    anomaly_id: int,
    depth: int = Query(3, ge=1, le=6),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AnomalyLineageOut:
    return AnomalyLineageOut(**_research.anomaly_lineage(db, anomaly_id=anomaly_id, depth=depth))


class MemoryGraphOut(BaseModel):
    nodes: List[AnomalyNode]
    edges: List[AnomalyEdge]
    edge_counts_by_kind: dict


@router.get("/research/memory-graph", response_model=MemoryGraphOut)
async def memory_graph_endpoint(
    limit_nodes: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MemoryGraphOut:
    return MemoryGraphOut(**_research.memory_graph(db, limit_nodes=limit_nodes))


class CrisisEvolutionState(BaseModel):
    state: str
    persist_count: int
    escalate_count: int
    stabilize_count: int
    total_transitions: int
    escalation_prob: float
    stabilization_prob: float


class CrisisEvolutionEdge(BaseModel):
    from_state: str
    to_state: str
    count: int


class CrisisEvolutionOut(BaseModel):
    since_ms: int
    states: List[CrisisEvolutionState]
    tree: List[CrisisEvolutionEdge]


@router.get("/research/crisis-evolution-tree", response_model=CrisisEvolutionOut)
async def crisis_evolution_tree_endpoint(
    lookback_days: int = Query(30, ge=3, le=180),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CrisisEvolutionOut:
    return CrisisEvolutionOut(**_research.crisis_evolution_tree(db, lookback_days=lookback_days))


class RegimeAncestryNode(BaseModel):
    regime: str
    count: int


class RegimeAncestryEdge(BaseModel):
    parent_regime: str
    child_regime: str
    weight: int


class RegimeAncestryOut(BaseModel):
    since_ms: int
    nodes: List[RegimeAncestryNode]
    edges: List[RegimeAncestryEdge]
    dominant_lineage: List[str]


@router.get("/research/regime-ancestry", response_model=RegimeAncestryOut)
async def regime_ancestry_endpoint(
    lookback_days: int = Query(30, ge=3, le=180),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> RegimeAncestryOut:
    return RegimeAncestryOut(**_research.regime_ancestry(db, lookback_days=lookback_days))


class EdgeLineagePoint(BaseModel):
    bucket_ts: int
    precision: Optional[float]
    total: int


class EdgeLineagePhase(BaseModel):
    phase: str
    start_ts: int
    end_ts: int


class EdgeLineageOut(BaseModel):
    kind: str
    since_ms: int
    bucket_days: int
    series: List[EdgeLineagePoint]
    phases: List[EdgeLineagePhase]
    origin_ts: Optional[int]


@router.get("/research/edge-lineage/{kind}", response_model=EdgeLineageOut)
async def edge_lineage_endpoint(
    kind: str,
    lookback_days: int = Query(60, ge=7, le=180),
    bucket_days: int = Query(7, ge=1, le=14),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> EdgeLineageOut:
    return EdgeLineageOut(**_research.edge_lineage(db, kind=kind, lookback_days=lookback_days, bucket_days=bucket_days))


class IntelligenceSnapshotRow(BaseModel):
    ts_ms: int
    synthesized_stress: Optional[float]
    coordinated_state: Optional[str]
    cross_layer_agreement: Optional[float]
    structural_break_score: Optional[float]
    meta_confidence_score: Optional[float]
    meta_intelligence_health: Optional[float]
    health_state: Optional[str]
    risk_state_score: Optional[float]
    regime_shift_probability: Optional[float]
    dominant_regime: Optional[str]


class IntelligenceHistoryOut(BaseModel):
    since_ms: Optional[int]
    count: int
    series: List[IntelligenceSnapshotRow]


@router.get("/research/intelligence-history", response_model=IntelligenceHistoryOut)
async def intelligence_history_endpoint(
    since_ms: Optional[int] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> IntelligenceHistoryOut:
    return IntelligenceHistoryOut(**_research.intelligence_history_series(db, since_ms=since_ms, limit=limit))


class CycleRun(BaseModel):
    phase: str
    start_ts: int
    end_ts: int
    duration_ms: int
    open: Optional[bool] = None


class CycleTransition(BaseModel):
    from_phase: str
    to_phase: str
    count: int
    probability: float


class MarketCycleOut(BaseModel):
    since_ms: int
    runs: List[CycleRun]
    current_phase: Optional[str]
    avg_duration_ms_per_phase: dict
    transition_matrix: List[CycleTransition]


@router.get("/research/market-cycle", response_model=MarketCycleOut)
async def market_cycle_endpoint(
    lookback_days: int = Query(60, ge=7, le=180),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MarketCycleOut:
    return MarketCycleOut(**_research.market_cycle(db, lookback_days=lookback_days))


class NarrativeChronicleOut(BaseModel):
    since_ms: int
    summary: str
    highlights: List[str]
    anomaly_count: int
    anomaly_counts_by_kind: Optional[dict] = None
    first_state: Optional[str] = None
    last_state: Optional[str] = None


@router.get("/research/narrative-chronicle", response_model=NarrativeChronicleOut)
async def narrative_chronicle_endpoint(
    lookback_days: int = Query(21, ge=7, le=90),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> NarrativeChronicleOut:
    return NarrativeChronicleOut(**_research.narrative_chronicle(db, lookback_days=lookback_days))


# ══════════════════════════════════════════════════════════════════════════
#  Phase-14 — Discovery endpoints
# ══════════════════════════════════════════════════════════════════════════


class DiscoveredPattern(BaseModel):
    discovered_pattern_id: str
    signature: dict
    support: int
    outcome_rate: float
    lift: Optional[float]
    effective_lift: float = 0.0
    dominant_alert_kind: Optional[str]
    dominant_alert_count: int
    novelty_score: float
    stability_score: float = 0.0
    pattern_confidence: float = 0.0
    robustness_flags: List[str] = []
    suppressed_reason: Optional[str] = None
    day_span: int = 0
    first_half_support: int = 0
    second_half_support: int = 0


class PatternDiscoveryOut(BaseModel):
    since_ms: int
    min_support: int
    bucket_minutes: int
    metrics: List[str]
    base_rate: float
    total_buckets: int
    patterns: List[DiscoveredPattern]
    data_quality: str = "INSUFFICIENT"
    suppressed_count: int = 0
    scarcity_factor: float = 0.0


@router.get("/research/pattern-discovery", response_model=PatternDiscoveryOut)
async def pattern_discovery_endpoint(
    since_ms: Optional[int] = Query(None),
    min_support: int = Query(12, ge=3, le=200),
    bucket_minutes: int = Query(30, ge=5, le=120),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> PatternDiscoveryOut:
    if since_ms is None:
        since_ms = int(time.time() * 1000) - 14 * 24 * 3600 * 1000
    return PatternDiscoveryOut(**_research.discover_patterns(
        db, since_ms=since_ms, min_support=min_support, bucket_minutes=bucket_minutes,
    ))


class Archetype(BaseModel):
    archetype_id: str
    archetype_label: str
    cluster_id: int
    size: int
    dominant_kind: str
    kinds: dict
    frequency_per_day: float
    avg_novelty: float
    escalation_profile: str
    recovery_probability: float
    structural_severity: float
    centroid: dict


class CrisisArchetypesOut(BaseModel):
    archetypes: List[Archetype]
    anomaly_count: int
    vocabulary: List[str]
    data_quality: str = "INSUFFICIENT"


@router.get("/research/crisis-archetypes", response_model=CrisisArchetypesOut)
async def crisis_archetypes_endpoint(
    max_archetypes: int = Query(8, ge=1, le=20),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> CrisisArchetypesOut:
    return CrisisArchetypesOut(**_research.crisis_archetypes(db, max_archetypes=max_archetypes))


class HiddenRegimeCluster(BaseModel):
    cluster_id: int
    label_hint: str
    size: int
    dominant_coordinated_state: Optional[str]
    earliest_ts: int
    latest_ts: int
    centroid: dict
    emergent_regime_score: float
    regime_stability: float
    is_emergent: bool


class HiddenRegimesOut(BaseModel):
    since_ms: int
    snapshot_count: int
    clusters: List[HiddenRegimeCluster]
    data_quality: str = "INSUFFICIENT"


@router.get("/research/hidden-regimes", response_model=HiddenRegimesOut)
async def hidden_regimes_endpoint(
    lookback_days: int = Query(30, ge=7, le=180),
    max_clusters: int = Query(6, ge=1, le=12),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> HiddenRegimesOut:
    return HiddenRegimesOut(**_research.hidden_regimes(db, lookback_days=lookback_days, max_clusters=max_clusters))


class PropagationEdge(BaseModel):
    from_symbol: str
    to_symbol: str
    count: int
    avg_lead_ms: float
    avg_lead_s: float
    lead_std_s: float = 0.0
    confidence: str    # HIGH | MEDIUM | LOW
    confidence_score: float = 0.0
    # Explainable sub-signals (all in [0, 1]).
    volume_strength: float = 0.0
    lead_clarity: float = 0.0
    lead_consistency: float = 0.0
    temporal_consistency: float = 0.0
    recurrence_stability: float = 0.0
    symmetry_penalty: float = 0.0
    leader_stability: float = 0.0
    base_confidence: float = 0.0


class PropagationNode(BaseModel):
    symbol: str
    out_count: int
    in_count: int
    net_lead: int
    leader_stability: float = 0.0
    follower_stability: float = 0.0


class PropagationIntegrityComponents(BaseModel):
    avg_confidence: float
    symmetric_share: float
    weak_share: float
    coverage: float


class PropagationOut(BaseModel):
    since_ms: int
    lead_window_ms: int
    edges: List[PropagationEdge]
    nodes: List[PropagationNode]
    systemic_contagion_score: float
    average_propagation_velocity_s: Optional[float]
    total_alerts: int
    integrity_score: float = 0.0
    integrity_components: Optional[PropagationIntegrityComponents] = None


@router.get("/research/propagation", response_model=PropagationOut)
async def propagation_endpoint(
    lookback_days: int = Query(14, ge=3, le=60),
    lead_window_minutes: int = Query(30, ge=5, le=180),
    min_lead_seconds: int = Query(5, ge=0, le=300),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> PropagationOut:
    return PropagationOut(**_research.propagation_graph(
        db,
        lookback_days=lookback_days,
        lead_window_ms=lead_window_minutes * 60_000,
        min_lead_ms=min_lead_seconds * 1000,
    ))


class EvolutionaryOut(BaseModel):
    lookback_days: int
    bucket_days: int
    behavioral_shift_rate: float
    instability_acceleration: float
    structural_maturity_score: float
    evolutionary_state: str   # DETERIORATION | INSTABILITY_GROWTH | SLOW_MUTATION | STABLE_MATURATION
    bad_directions: int
    metric_trends: List[EvolutionMetric]
    regime_entropy_series: List[EntropyPoint]


@router.get("/research/evolutionary-behavior", response_model=EvolutionaryOut)
async def evolutionary_behavior_endpoint(
    lookback_days: int = Query(60, ge=14, le=180),
    bucket_days: int = Query(7, ge=1, le=14),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> EvolutionaryOut:
    return EvolutionaryOut(**_research.evolutionary_behavior(
        db, lookback_days=lookback_days, bucket_days=bucket_days,
    ))


class AbstractionRow(BaseModel):
    archetype_id: str
    label: str
    share_of_memory: float
    members: int
    frequency_per_day: float
    structural_severity: float


class MemoryAbstractionOut(BaseModel):
    total_anomalies: int
    abstractions: List[AbstractionRow]
    memory_density_score: float


@router.get("/research/memory-abstraction", response_model=MemoryAbstractionOut)
async def memory_abstraction_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> MemoryAbstractionOut:
    return MemoryAbstractionOut(**_research.memory_abstraction(db))


class ForecastEntry(BaseModel):
    metric: str
    current: float
    slope_per_day: float
    raw_slope_per_day: float = 0.0
    slope_capped: bool = False
    extrapolation_capped: bool = False
    slope_consistency: float = 0.0
    forecast_in_days: int
    forecast_value: float
    rmse: float
    horizon_decay: float = 1.0
    confidence: float
    data_quality: str = "INSUFFICIENT"


class IntelligenceForecastOut(BaseModel):
    horizon_days: int
    forecasts: List[ForecastEntry]
    trajectory: Optional[str] = None
    snapshot_count: int
    data_quality: str = "INSUFFICIENT"


@router.get("/research/intelligence-forecast", response_model=IntelligenceForecastOut)
async def intelligence_forecast_endpoint(
    horizon_days: int = Query(7, ge=1, le=30),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> IntelligenceForecastOut:
    return IntelligenceForecastOut(**_research.intelligence_evolution_forecast(db, horizon_days=horizon_days))


class AdaptationRec(BaseModel):
    action: str    # STRENGTHEN | WEAKEN | TIGHTEN_THRESHOLD | LOOSEN_THRESHOLD | REWEIGHT_UNSTABLE | STRENGTHEN_EDGE
    target: str
    rationale: str
    importance_shift: float


class AdaptationOut(BaseModel):
    fetched_at_ms: int
    recommendations: List[AdaptationRec]
    adaptation_score: float


@router.get("/research/adaptation-recommendations", response_model=AdaptationOut)
async def adaptation_recommendations_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> AdaptationOut:
    return AdaptationOut(**_research.adaptation_recommendations(db))


class SanityFinding(BaseModel):
    kind: str          # validation_collapse | anomaly_inflation | propagation_loop | forecast_overshoot | pattern_explosion | unstable_clustering
    severity: str      # critical | warn | info
    detail: str


class SanityAuditOut(BaseModel):
    fetched_at_ms: int
    overall_state: str  # CRITICAL | WARN | INFO | CLEAN
    findings: List[SanityFinding]
    check_count: int


@router.get("/research/sanity-audit", response_model=SanityAuditOut)
async def sanity_audit_endpoint(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> SanityAuditOut:
    return SanityAuditOut(**_research.sanity_audit(db))
